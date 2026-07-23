"""SP-6: py / dll ステップ + ポリシーゲートのテスト (v2.32.0)

対象:
- py: code 正常 / outputs 選別 / 未宣言 output は取り込まない / エラー /
  タイムアウト / file + main() / sha256 記録 / ndarray 入出力
- dll: msvcrt.abs で正常 / 型宣言必須 / 存在しない関数 / ワーカー死の回収
- ポリシー: deny / scripts_dir_only / hash_pinned / dll_dirs 空 = 事実上 deny
- contains_code の asset.yaml 記載 + checker 表示
- dry-run 不透明ステップ
- 後方互換
"""
import asyncio
import sys
import textwrap
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest
import yaml

from lab_executor.code_policy import (
    CodePolicy, CodePolicyError, check_dll, check_python,
    load_policy, sha256_file, sha256_text,
)
from lab_executor.models.instrument_def import InstrumentDefinition
from lab_executor.instrument_registry import InstrumentRegistry
from lab_executor.recipe_executor import execute_recipe, recipe_to_plan
from lab_executor.experiment_ir import DllStep, PyStep
from lab_executor.utils.seq_expression import SeqExpressionError
from lab_executor.ui.views import dryrun_view
from lab_visa_mcp.session_manager import InstrumentSession

RESOURCE = "TEST::INSTR"
MSVCRT = "C:/Windows/System32/msvcrt.dll"

BASE_YAML = """
metadata:
  manufacturer: "Test"
  model: "Sp6Rig"
response_formats:
  num:
    fallback: "numeric_extract"
commands:
  meas:
    scpi: "MEAS?"
    type: "query"
    returns: { type: "float", format: "num" }
recipes: {}
"""


def _defn(recipes: dict) -> InstrumentDefinition:
    doc = yaml.safe_load(textwrap.dedent(BASE_YAML))
    doc["recipes"] = recipes
    return InstrumentDefinition(**doc)


def _session(defn):
    return InstrumentSession(
        resource_name=RESOURCE,
        idn_response="<test>",
        idn_parsed={"manufacturer": "Test", "model": "Sp6Rig"},
        definition=defn,
    )


def _visa(query_return="3.0"):
    v = MagicMock()
    v.write = AsyncMock(return_value=None)
    v.query = AsyncMock(return_value=query_return)
    return v


def _write_policy(tmp_path: Path, text: str) -> Path:
    (tmp_path / "_policy.yaml").write_text(
        textwrap.dedent(text), encoding="utf-8",
    )
    return tmp_path


@pytest.fixture(autouse=True)
def _clear_policy_env(monkeypatch):
    monkeypatch.delenv("LAB_EXECUTOR_POLICY_DIR", raising=False)
    monkeypatch.setenv("VISA_MCP_SAFETY_MODE", "permissive")


@pytest.fixture
def allow_python(tmp_path, monkeypatch):
    """python 実行を明示的に許可する。

    既定は deny なので、コード実行そのものを検証するテストは、運用時と同じく
    ポリシーで明示的に許可する必要がある。許可を書かずに動いてしまうと、
    既定が緩んだことに誰も気付けない。
    """
    _write_policy(tmp_path, """
        code_execution:
          python: allow
    """)
    monkeypatch.setenv("LAB_EXECUTOR_POLICY_DIR", str(tmp_path))
    return tmp_path


PY_CODE_RECIPE = {
    "pycalc": {
        "steps": [
            {"command": "meas", "result_as": "x"},
            {"py": {
                "code": (
                    "r = ctx['x'] * 2 + ctx['params'].get('offset', 0)\n"
                    "out['doubled'] = r\n"
                    "out['ignored'] = 999\n"
                ),
                "inputs": {"x": "steps.x"},
                "outputs": ["doubled"],
                "timeout_s": 30,
            }},
            {"compute": {"set": "y", "expr": "vars.doubled + 1"}},
        ],
    },
}


# ============================================================
# 1. py 実行
# ============================================================

@pytest.mark.asyncio
async def test_py_code_outputs_and_filtering(allow_python):
    defn = _defn(PY_CODE_RECIPE)
    plan = recipe_to_plan(defn.recipes["pycalc"], {}, definition=defn)
    ps = plan.steps[1]
    assert isinstance(ps, PyStep)
    assert len(ps.sha256) == 64          # sha256 がコンパイル時に記録される

    res = await execute_recipe(_visa("3.0"), _session(defn), "pycalc", {})
    assert res["success"] is True, res
    # outputs 宣言分のみ vars へ (ignored は取り込まれない)
    assert res["variables"]["vars"]["doubled"] == 6.0
    assert "ignored" not in res["variables"]["vars"]
    assert res["variables"]["vars"]["y"] == 7.0
    pstep = res["steps_executed"][1]
    assert pstep["step_type"] == "py"
    assert pstep["sha256"] == ps.sha256


@pytest.mark.asyncio
async def test_py_error_and_missing_output(allow_python):
    defn = _defn({
        "pyerr": {
            "steps": [
                {"py": {"code": "raise RuntimeError('boom')",
                        "outputs": [], "timeout_s": 30}},
            ],
        },
        "pymissing": {
            "steps": [
                {"py": {"code": "out['a'] = 1",
                        "outputs": ["b"], "timeout_s": 30}},
            ],
        },
    })
    res = await execute_recipe(_visa(), _session(defn), "pyerr", {})
    assert res["success"] is False
    failed = res["steps_executed"][-1]
    assert failed["error"] == "py_RuntimeError"
    assert "boom" in failed["message"]
    assert "traceback" in failed

    res2 = await execute_recipe(_visa(), _session(defn), "pymissing", {})
    assert res2["success"] is False
    assert "b" in res2["steps_executed"][-1]["message"]


@pytest.mark.asyncio
async def test_py_timeout(allow_python):
    defn = _defn({
        "pyslow": {
            "steps": [
                {"py": {"code": "import time\ntime.sleep(60)",
                        "outputs": [], "timeout_s": 2}},
            ],
        },
    })
    res = await execute_recipe(_visa(), _session(defn), "pyslow", {})
    assert res["success"] is False
    failed = res["steps_executed"][-1]
    assert failed["error"] == "py_timeout"


@pytest.mark.asyncio
async def test_py_worker_is_terminated_when_parent_task_is_cancelled(tmp_path):
    from lab_executor.code_exec import run_py

    marker = tmp_path / "worker-survived.txt"
    code = (
        "import time\n"
        "from pathlib import Path\n"
        "time.sleep(0.8)\n"
        f"Path({str(marker)!r}).write_text('survived', encoding='utf-8')\n"
    )
    task = asyncio.create_task(run_py(
        code=code,
        file_path=None,
        inputs={},
        outputs=[],
        params={},
        env={},
        timeout_s=10,
    ))
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(1.0)

    assert not marker.exists()


@pytest.mark.asyncio
async def test_py_file_with_main_and_sha256_event(tmp_path, monkeypatch):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    script = scripts / "fit.py"
    script.write_text(textwrap.dedent("""
        import numpy as np
        def main(ctx):
            arr = ctx["xs"]
            return {"total": float(np.sum(arr)), "scaled": arr * 2}
    """), encoding="utf-8")
    _write_policy(tmp_path, """
        code_execution:
          python: scripts_dir_only
          scripts_dir: ./scripts
    """)
    monkeypatch.setenv("LAB_EXECUTOR_POLICY_DIR", str(tmp_path))

    defn = _defn({
        "pyfile": {
            "steps": [
                {"repeat": {
                    "count": 3, "collect": {"v": "xs"},
                    "steps": [{"command": "meas", "result_as": "v"}],
                }},
                {"py": {"file": "fit.py",
                        "inputs": {"xs": "vars.xs"},
                        "outputs": ["total", "scaled"],
                        "timeout_s": 30}},
            ],
        },
    })
    plan = recipe_to_plan(defn.recipes["pyfile"], {}, definition=defn)
    ps = plan.steps[1]
    assert ps.resolved_path.endswith("fit.py")
    assert ps.sha256 == sha256_file(script)

    res = await execute_recipe(_visa("2.0"), _session(defn), "pyfile", {})
    assert res["success"] is True, res
    # ndarray 入出力 (npy 経由): total はスカラ、scaled は array (要約形で記録)
    assert res["variables"]["vars"]["total"] == 6.0
    assert res["variables"]["vars"]["scaled"]["__type__"] == "array"
    assert res["variables"]["vars"]["scaled"]["head"] == [4.0, 4.0, 4.0]


# ============================================================
# 2. ポリシーゲート
# ============================================================

def test_policy_deny_python(tmp_path, monkeypatch):
    _write_policy(tmp_path, """
        code_execution:
          python: deny
    """)
    monkeypatch.setenv("LAB_EXECUTOR_POLICY_DIR", str(tmp_path))
    defn = _defn(PY_CODE_RECIPE)
    with pytest.raises(SeqExpressionError, match="拒否"):
        recipe_to_plan(defn.recipes["pycalc"], {}, definition=defn)


def test_policy_scripts_dir_only_rejects_code(tmp_path, monkeypatch):
    _write_policy(tmp_path, """
        code_execution:
          python: scripts_dir_only
          scripts_dir: ./scripts
    """)
    monkeypatch.setenv("LAB_EXECUTOR_POLICY_DIR", str(tmp_path))
    defn = _defn(PY_CODE_RECIPE)   # code: インライン
    with pytest.raises(SeqExpressionError, match="scripts_dir"):
        recipe_to_plan(defn.recipes["pycalc"], {}, definition=defn)


def test_policy_hash_pinned(tmp_path, monkeypatch):
    code = (
        "r = ctx['x'] * 2 + ctx['params'].get('offset', 0)\n"
        "out['doubled'] = r\n"
        "out['ignored'] = 999\n"
    )
    digest = sha256_text(code)
    _write_policy(tmp_path, f"""
        code_execution:
          python: hash_pinned
          pinned_hashes: ["sha256:{digest}"]
    """)
    monkeypatch.setenv("LAB_EXECUTOR_POLICY_DIR", str(tmp_path))
    defn = _defn(PY_CODE_RECIPE)
    plan = recipe_to_plan(defn.recipes["pycalc"], {}, definition=defn)
    assert isinstance(plan.steps[1], PyStep)
    # pin されていないコードは拒否
    defn2 = _defn({
        "other": {"steps": [{"py": {"code": "out['z'] = 1",
                                    "outputs": ["z"], "timeout_s": 5}}]},
    })
    with pytest.raises(SeqExpressionError, match="pinned"):
        recipe_to_plan(defn2.recipes["other"], {}, definition=defn2)


def _write_policy_instrument(tmp_path: Path) -> InstrumentDefinition:
    raw = {
        "metadata": {"manufacturer": "Test", "model": "PolicyRig"},
        "recipes": {
            "code": {
                "steps": [{
                    "py": {
                        "code": "out['v'] = 1",
                        "outputs": ["v"],
                        "timeout_s": 5,
                    },
                }],
            },
        },
    }
    (tmp_path / "policy_rig.yaml").write_text(
        yaml.safe_dump(raw, sort_keys=False), encoding="utf-8",
    )
    definition = InstrumentRegistry(tmp_path).get_definition("Test", "PolicyRig")
    assert definition is not None
    return definition


def test_registry_definition_uses_adjacent_policy_at_compile_time(tmp_path):
    _write_policy(tmp_path, """
        code_execution:
          python: deny
    """)
    definition = _write_policy_instrument(tmp_path)

    with pytest.raises(SeqExpressionError, match="deny"):
        recipe_to_plan(definition.recipes["code"], {}, definition=definition)


@pytest.mark.asyncio
async def test_adjacent_policy_is_reloaded_for_runtime_recheck(tmp_path):
    _write_policy(tmp_path, """
        code_execution:
          python: allow
    """)
    definition = _write_policy_instrument(tmp_path)
    plan = recipe_to_plan(
        definition.recipes["code"], {}, definition=definition,
    )
    assert plan.steps[0].policy_dir == str(tmp_path.resolve())

    # 管理者がcompile後にdenyへ変更した場合も、実行直前ゲートで止める。
    _write_policy(tmp_path, """
        code_execution:
          python: deny
    """)
    from lab_executor import seq_runtime
    from lab_executor.experiment_ir import VariableStore

    result = await seq_runtime.process_py_step(
        plan.steps[0], VariableStore(params={}, env={}),
    )
    assert result["success"] is False
    assert result["error"] == "policy_violation"


def test_policy_defaults_are_closed():
    # 既定ポリシー: python=deny / dll=dir_allowlist + dll_dirs 空 = 事実上 deny。
    # ポリシー未設定の環境で、取り込んだ定義中のコードが走らないこと。
    pol = load_policy(None)
    assert pol.python == "deny"
    assert pol.dll == "dir_allowlist" and pol.dll_dirs == []
    with pytest.raises(CodePolicyError, match="dll_dirs"):
        check_dll(pol, path=Path(MSVCRT), sha256="0" * 64)
    # python も既定で拒否される (以前は allow だった)
    with pytest.raises(CodePolicyError, match="python=deny"):
        check_python(pol, file_path=None, sha256="0" * 64)


# ============================================================
# 3. dll 実行 (msvcrt.abs)
# ============================================================

def _dll_policy(tmp_path, monkeypatch):
    _write_policy(tmp_path, """
        code_execution:
          python: allow
          dll: dir_allowlist
          dll_dirs: ["C:/Windows/System32"]
    """)
    monkeypatch.setenv("LAB_EXECUTOR_POLICY_DIR", str(tmp_path))


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 専用 (msvcrt)")
@pytest.mark.asyncio
async def test_dll_abs_call(tmp_path, monkeypatch):
    _dll_policy(tmp_path, monkeypatch)
    defn = _defn({
        "dllcalc": {
            "steps": [
                {"command": "meas", "result_as": "x"},
                {"dll": {
                    "path": MSVCRT,
                    "function": "abs",
                    "argtypes": ["int"],
                    "restype": "int",
                    "args": ["${0 - steps.x * 2}"],
                    "result_as": "absval",
                    "timeout_s": 30,
                }},
                {"compute": {"set": "y", "expr": "steps.absval + 1"}},
            ],
        },
    })
    plan = recipe_to_plan(defn.recipes["dllcalc"], {}, definition=defn)
    dstep = plan.steps[1]
    assert isinstance(dstep, DllStep)
    assert dstep.sha256 == sha256_file(Path(MSVCRT))

    res = await execute_recipe(_visa("3.0"), _session(defn), "dllcalc", {})
    assert res["success"] is True, res
    assert res["variables"]["steps"]["absval"] == 6    # abs(-6)
    assert res["variables"]["vars"]["y"] == 7


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 専用 (msvcrt)")
@pytest.mark.asyncio
async def test_dll_missing_function_and_type_decl_required(tmp_path, monkeypatch):
    _dll_policy(tmp_path, monkeypatch)
    # 存在しない関数 → ワーカーから AttributeError が返りステップ failed
    defn = _defn({
        "dllbad": {
            "steps": [
                {"dll": {"path": MSVCRT, "function": "no_such_function_xyz",
                         "argtypes": [], "restype": "int", "args": [],
                         "timeout_s": 30}},
            ],
        },
    })
    res = await execute_recipe(_visa(), _session(defn), "dllbad", {})
    assert res["success"] is False
    assert "no_such_function_xyz" in res["steps_executed"][-1]["message"]

    # 型宣言 (argtypes / restype) は必須 → schema 検証エラー
    with pytest.raises(Exception, match="argtypes"):
        _defn({
            "nodecl": {
                "steps": [
                    {"dll": {"path": MSVCRT, "function": "abs",
                             "restype": "int"}},
                ],
            },
        })
    with pytest.raises(Exception, match="restype"):
        _defn({
            "nodecl2": {
                "steps": [
                    {"dll": {"path": MSVCRT, "function": "abs",
                             "argtypes": ["int"], "restype": ""}},
                ],
            },
        })


@pytest.mark.asyncio
async def test_worker_crash_is_recovered(tmp_path, monkeypatch, allow_python):
    # py コードでワーカーを即死させる (os._exit) → worker_crashed で回収
    defn = _defn({
        "pycrash": {
            "steps": [
                {"py": {"code": "import os\nos._exit(3)",
                        "outputs": [], "timeout_s": 30}},
            ],
        },
    })
    res = await execute_recipe(_visa(), _session(defn), "pycrash", {})
    assert res["success"] is False
    failed = res["steps_executed"][-1]
    assert failed["error"] == "py_worker_crashed"
    assert "異常終了" in failed["message"]


@pytest.mark.asyncio
async def test_runtime_sha256_recheck(tmp_path, monkeypatch):
    # コンパイル後にファイルを書き換える → 実行直前の sha256 再照合で failed
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    script = scripts / "calc.py"
    script.write_text("out['v'] = 1\n", encoding="utf-8")
    _write_policy(tmp_path, """
        code_execution:
          python: allow
          scripts_dir: ./scripts
    """)
    monkeypatch.setenv("LAB_EXECUTOR_POLICY_DIR", str(tmp_path))
    defn = _defn({
        "pyfile2": {
            "steps": [
                {"py": {"file": "calc.py", "outputs": ["v"], "timeout_s": 30}},
            ],
        },
    })
    plan = recipe_to_plan(defn.recipes["pyfile2"], {}, definition=defn)
    # TOCTOU: コンパイル後の改変
    script.write_text("out['v'] = 999\n", encoding="utf-8")
    from lab_executor.experiment_ir import VariableStore
    from lab_executor import seq_runtime
    store = VariableStore(params={}, env={})
    result = asyncio.get_event_loop().run_until_complete(
        seq_runtime.process_py_step(plan.steps[0], store),
    ) if False else await seq_runtime.process_py_step(plan.steps[0], store)
    assert result["success"] is False
    assert result["error"] == "policy_violation"
    assert "sha256" in result["message"]


@pytest.mark.asyncio
async def test_py_file_executes_verified_snapshot_when_path_is_swapped(
    tmp_path, monkeypatch,
):
    """The worker must not reopen a file after the runtime hash check."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    script = scripts / "calc.py"
    script.write_text("out['v'] = 1\n", encoding="utf-8")
    _write_policy(tmp_path, """
        code_execution:
          python: allow
          scripts_dir: ./scripts
    """)
    monkeypatch.setenv("LAB_EXECUTOR_POLICY_DIR", str(tmp_path))
    defn = _defn({
        "pyfile_snapshot": {
            "steps": [
                {"py": {"file": "calc.py", "outputs": ["v"], "timeout_s": 30}},
            ],
        },
    })
    plan = recipe_to_plan(
        defn.recipes["pyfile_snapshot"], {}, definition=defn,
    )

    from lab_executor import code_exec, seq_runtime
    from lab_executor.experiment_ir import VariableStore

    original_run_py = code_exec.run_py

    async def swap_source_then_run(**kwargs):
        script.write_text("out['v'] = 999\n", encoding="utf-8")
        return await original_run_py(**kwargs)

    monkeypatch.setattr(code_exec, "run_py", swap_source_then_run)
    store = VariableStore(params={}, env={})
    result = await seq_runtime.process_py_step(plan.steps[0], store)

    assert result["success"] is True, result
    assert store.as_ctx()["vars"]["v"] == 1


# ============================================================
# 4. contains_code (資産表示)
# ============================================================

def test_contains_code_detection_and_manifest():
    from lab_executor.asset.builder import _detect_contains_code
    # py を含む (branch 内も検出)
    frag = {"definition": {"steps": [
        {"command": "meas"},
        {"branch": [
            {"when": "steps.x > 1",
             "steps": [{"py": {"code": "out['a']=1", "outputs": ["a"]}}]},
        ]},
    ]}}
    assert _detect_contains_code(frag) == {"python": True, "dll": False}
    # dll を含む (repeat 内)
    frag2 = {"definition": {"steps": [
        {"repeat": {"count": 2, "steps": [
            {"dll": {"path": "x.dll", "function": "f",
                     "argtypes": [], "restype": "void"}},
        ]}},
    ]}}
    assert _detect_contains_code(frag2) == {"python": False, "dll": True}
    # コードなし → None (asset.yaml にキーを載せない)
    assert _detect_contains_code(
        {"definition": {"steps": [{"command": "meas"}]}}) is None
    assert _detect_contains_code(None) is None


def test_contains_code_in_checker_report(tmp_path):
    """contains_code 付き asset.yaml が schema を通り、check レポートに載る。"""
    import hashlib
    import zipfile
    from lab_executor.asset.checker import check_asset

    files = {
        "bundle/results.jsonl": b'{"value": 1.0}\n',
    }
    contents = [
        {"path": k, "sha256": hashlib.sha256(v).hexdigest(), "kind": "results"}
        for k, v in files.items()
    ]
    manifest = {
        "asset_version": "0.1",
        "asset_id": "test-cc-asset",
        "level_declared": 0,
        "contains_code": {"python": True, "dll": False},
        "contents": contents,
    }
    zp = tmp_path / "cc.asset.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        zf.writestr("asset.yaml", yaml.safe_dump(manifest))
        for k, v in files.items():
            zf.writestr(k, v)
    rep = check_asset(zp)
    assert rep.schema_ok is True
    assert rep.contains_code == {"python": True, "dll": False}
    assert rep.to_dict()["contains_code"] == {"python": True, "dll": False}


# ============================================================
# 5. dry-run (不透明ステップ)
# ============================================================

def test_dryrun_opaque_py_dll(tmp_path, monkeypatch):
    _dll_policy(tmp_path, monkeypatch)
    steps = [
        {"py": {"code": "out['fit'] = 1.0", "outputs": ["fit"],
                "timeout_s": 30}},
        {"compute": {"set": "z", "expr": "vars.fit * 2"}},
    ]
    if sys.platform == "win32":
        steps.insert(1, {"dll": {
            "path": MSVCRT, "function": "abs",
            "argtypes": ["int"], "restype": "int",
            "args": [1], "result_as": "a", "timeout_s": 5,
        }})
    defn = _defn({"an": {"steps": steps}})
    plan = recipe_to_plan(defn.recipes["an"], {}, definition=defn)
    view = dryrun_view(plan)
    py_row = view["steps"][0]
    assert py_row["type"] == "py" and py_row["opaque"] is True
    assert py_row["outputs"] == ["fit"]
    assert py_row["has_code"] is True
    if sys.platform == "win32":
        dll_row = view["steps"][1]
        assert dll_row["type"] == "dll" and dll_row["opaque"] is True
        assert dll_row["function"] == "abs"
    # outputs の test_values を与えて後続を評価できる
    view2 = dryrun_view(plan, test_values={"vars.fit": 1.5})
    comp = view2["steps"][-1]
    assert comp["value"] == 3.0
    import json
    json.dumps(view2)


# ============================================================
# 6. 後方互換
# ============================================================

@pytest.mark.asyncio
async def test_backward_compat_no_code_steps():
    defn = _defn({
        "plain": {
            "steps": [
                {"command": "meas", "result_as": "x"},
                {"compute": {"set": "y", "expr": "steps.x * 2"}},
            ],
        },
    })
    res = await execute_recipe(_visa("2.5"), _session(defn), "plain", {})
    assert res["success"] is True
    assert res["variables"]["vars"]["y"] == 5.0
    # コード無しレシピの contains_code は None
    from lab_executor.asset.builder import _detect_contains_code
    assert _detect_contains_code(
        {"definition": {"steps": defn.recipes["plain"].model_dump()["steps"]}}
    ) is None


@pytest.mark.asyncio
async def test_python_is_refused_when_no_policy_file_exists():
    """ポリシー未設定の環境で、定義に含まれる Python が実行されないこと。

    外部から受け取った機器定義や extension を取り込む運用では、これが
    任意コード実行の入口になる。実行環境は隔離サンドボックスではないため、
    既定は拒否でなければならない (v2.38.0 で allow から変更)。
    """
    defn = _defn(PY_CODE_RECIPE)
    res = await execute_recipe(_visa("3.0"), _session(defn), "pycalc", {})
    assert res["success"] is False
    # コンパイル段階で拒否されるため、1 ステップも実行されない
    assert res["steps_executed"] == []
    assert "python=deny" in res["message"]
