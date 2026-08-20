"""
Recipe 実行エンジン (v0.5.0-rc1 で IR ベースに refactor)

設計:
- YAML の `RecipeDefinition` を内部 IR (`experiment_ir.Plan`) に変換
- Plan を `execute_plan()` が walk して実行
- v0.5.0-rc1: CommandStep (従来の機器コマンド) + WaitStep (asyncio.sleep) のみ
- v0.5.1 以降で wait_for_* 系 step が追加されても execute_plan のディスパッチを増やすだけ

外部 API (`execute_recipe`) の戻り値形式は v0.3.0 までと互換性を維持:
- `{"success": bool, "recipe": str, "steps_executed": [...], "step_count": N}`
- 失敗時は `{"success": False, ..., "halted_at_step": idx}`

新しい標準 envelope (`response_envelope.make_envelope`) は v0.5.0+ で新規追加される
MCP ツール (Job 系等) で採用する。既存 `execute_recipe` ツールは後方互換のため従来形式。
"""
from __future__ import annotations
import asyncio
import logging
from typing import Any, TYPE_CHECKING

from .experiment_ir import (
    CommandStep, ComputeStep, Plan, Step, WaitStep,
    WaitUntilStep, WaitForConditionStep, WaitForStableStep,
    BarrierStep, VariableStore,
    GuardStep, BranchCase, BranchStep, RepeatStep, PauseStep,
    PyStep, DllStep, CallStep,
)

if TYPE_CHECKING:
    from .code_policy import CodePolicy
from .models.instrument_def import InstrumentDefinition, RecipeDefinition, RecipeStep
from .session import SessionResolver
from .step_executor import execute_command_step, execute_wait_step
from .utils.expression import resolve_arg, ExpressionError
from .utils.seq_expression import (
    SeqExpressionError, check_expr, parse_deferred, referenced_names,
    string_expr_parts,
)
from . import seq_runtime


# v2.0: backend layer は visa-mcp / shim 経由。型ヒント目的
# は TYPE_CHECKING、VisaError は ImportError fallback。
if TYPE_CHECKING:
    from lab_visa_mcp.session_manager import InstrumentSession
    from lab_visa_mcp.visa_manager import VisaManager

try:
    from lab_visa_mcp.visa_manager import VisaError
except ImportError:
    class VisaError(Exception):  # type: ignore[no-redef]
        """visa-mcp 不在時の VisaError 代替"""
        pass

logger = logging.getLogger(__name__)


# ============================================================
# Recipe → IR Plan 変換
# ============================================================

# 予約 env 名 (実行時に VariableStore が供給する)。
# loop_index は repeat body 内でのみ利用可 (SP-3)。
_ENV_NAMES = frozenset({"job_id", "started_at"})

# v2.29.0 (SP-3): 展開・ネスト上限 (spec §10「レビュー可能性の上限」)。
# 値は docs/sequence_processing.md に記載。
BRANCH_MAX_DEPTH = 3            # branch のネスト最大深さ
REPEAT_MAX_COUNT = 10_000       # repeat count / max_iterations の上限
MAX_TOTAL_STEPS_ESTIMATE = 100_000  # 展開後総ステップ数の静的見積り上限
CALL_MAX_DEPTH = 5             # v2.33.0 (SP-7): サブシーケンス呼び出しのネスト最大深さ


def _validate_expr_refs(
    expr: str,
    *,
    defined_steps: set[str],
    defined_vars: set[str],
    param_names: set[str],
    context: str,
    env_names: frozenset[str] | set[str] = _ENV_NAMES,
) -> None:
    """compute / ${...} / branch when / guard / repeat while の式をコンパイル時検証する。

    - 構文 + AST ホワイトリスト (check_expr)
    - 参照名がその時点までに定義される名前に含まれること (前方参照禁止)
    - env_names: その文脈で参照可能な env 予約名 (repeat body 内は + loop_index)
    """
    try:
        check_expr(expr)
        refs = referenced_names(expr)
    except SeqExpressionError as e:
        raise SeqExpressionError(f"{context}: {e}")

    tables = {
        "params": param_names,
        "steps": defined_steps,
        "vars": defined_vars,
        "env": set(env_names),
    }
    for ref in refs:
        if "." in ref:
            ns, name = ref.split(".", 1)
            table = tables.get(ns)
            if table is None or name not in table:
                raise SeqExpressionError(
                    f"{context}: 未定義または前方参照の変数 '{ref}'"
                )
        else:
            # 裸名: params / 先行 steps / 先行 vars のいずれかに存在する必要がある
            if (
                ref not in param_names
                and ref not in defined_steps
                and ref not in defined_vars
            ):
                raise SeqExpressionError(
                    f"{context}: 未定義または前方参照の変数 '{ref}'"
                )


def _resolve_range_decl(
    recipe: RecipeDefinition,
    definition: InstrumentDefinition | None,
    command: str,
    arg: str,
) -> tuple[float | None, float | None]:
    """実行時解決 arg の範囲宣言を集約する (安全要件の核, spec §6)。

    ParameterDefinition.range と recipe.requires.ranges の両方を見て、
    どちらも無ければ検証エラー。両方あれば厳しい方 (積集合) を返す。
    """
    mins: list[float] = []
    maxs: list[float] = []
    declared = False

    ranges_decl = (recipe.requires.ranges if recipe.requires else {}) or {}
    rspec = ranges_decl.get(f"{command}.{arg}")
    if rspec is not None:
        declared = True
        if rspec.min is not None:
            mins.append(float(rspec.min))
        if rspec.max is not None:
            maxs.append(float(rspec.max))

    if definition is not None:
        cmd_def = definition.commands.get(command)
        if cmd_def is not None:
            for pdef in cmd_def.parameters:
                if pdef.name == arg and pdef.range:
                    declared = True
                    mins.append(float(pdef.range[0]))
                    maxs.append(float(pdef.range[1]))
                    break

    if not declared:
        raise SeqExpressionError(
            f"実行時解決引数 '{command}.{arg}' に範囲宣言がありません。"
            "ParameterDefinition.range または recipe.requires.ranges "
            f"('{command}.{arg}') のいずれかが必須です (安全要件 §6)"
        )
    return (max(mins) if mins else None, min(maxs) if maxs else None)


def _resolve_sequences(
    definition: "InstrumentDefinition | None",
    sequences_dir: str | None,
) -> dict[str, Any]:
    """v2.33.0 (SP-7): サブシーケンス解決辞書を構築する。

    - 同一 definition の ``sequences``: キー ``"<名前>"``
    - ``sequences_dir`` 内の ``*.yaml``: キー ``"<ファイル stem>.<名前>"``
      + ライブラリファイルの sha256 を来歴用に保持

    返り値: {解決キー: {"seq": SubsequenceDefinition, "sha256": str}}
    同名衝突 (同一キーが複数ソース) は SeqExpressionError。
    """
    import yaml
    from pathlib import Path
    from .code_policy import sha256_text
    from .models.instrument_def import SubsequenceDefinition

    out: dict[str, Any] = {}
    if definition is not None:
        for name, seq in (definition.sequences or {}).items():
            out[name] = {"seq": seq, "sha256": ""}
    if sequences_dir:
        d = Path(sequences_dir)
        if d.is_dir():
            for f in sorted(d.glob("*.yaml")) + sorted(d.glob("*.yml")):
                text = f.read_text(encoding="utf-8")
                digest = sha256_text(text)
                data = yaml.safe_load(text) or {}
                seqs = data.get("sequences") or {}
                stem = f.stem
                for name, raw in seqs.items():
                    key = f"{stem}.{name}"
                    if key in out:
                        raise SeqExpressionError(
                            f"サブシーケンス名が衝突しています: {key!r} "
                            f"(ファイル {f.name})"
                        )
                    out[key] = {
                        "seq": SubsequenceDefinition.model_validate(raw),
                        "sha256": digest,
                    }
    return out


def recipe_to_plan(
    recipe: RecipeDefinition,
    variables: dict[str, Any],
    *,
    primary_resource: str | None = None,
    definition: InstrumentDefinition | None = None,
    policy: "CodePolicy | None" = None,
    sequences_dir: str | None = None,
    session_resolver: SessionResolver | None = None,
) -> Plan:
    """
    YAML の RecipeDefinition + 変数辞書 → IR Plan に変換する。
    args 内の `$var` / `$var * 1.1` 等の式は事前に評価して具体値にする。
    `${steps.x}` 形式は実行時解決 (deferred) として保持する (SP-2)。

    primary_resource を渡すと Plan.required_resources の起点となる。
    polling 系 step が別 instrument を参照する場合、そのリソースも required_resources に追加される。
    canonical sorted 順で deduplicate。

    definition (機器定義) は ${...} の範囲宣言検証 (ParameterDefinition.range)
    に使う。deferred arg があるのに definition が無く requires.ranges にも
    宣言が無い場合は SeqExpressionError。

    v2.29.0 (SP-3): branch / repeat / guard をネスト構造を保った IR
    (BranchStep.cases[].steps / RepeatStep.body が Step のリスト) に変換する。
    ネスト内では全経路定義検証・branch 深さ上限・展開見積り上限を執行する。

    v2.32.0 (SP-6): py / dll ステップはコンパイル時に path 解決・sha256 計算・
    **ポリシーゲート** (code_policy) 検証を行う。``policy`` 省略時は
    ``load_policy()`` (env ``LAB_EXECUTOR_POLICY_DIR`` の ``_policy.yaml``、
    無ければ既定ポリシー) を使う。
    """
    aux_resources: set[str] = set()

    # Registry経由の定義はロード元dirを保持する。env設定を要求せず、仕様どおり
    # instruments dir直下の_policy.yamlをコンパイル時・実行時の起点にする。
    if policy is None and definition is not None:
        source_dir = getattr(definition, "_source_dir", None)
        if source_dir:
            from .code_policy import load_policy
            policy = load_policy(source_dir)

    # コンパイル時検証用: その時点までに定義される名前を追跡する
    param_names: set[str] = set(variables.keys()) | {p.name for p in recipe.parameters}

    sequences = _resolve_sequences(definition, sequences_dir)
    conv = _StepConverter(
        recipe=recipe, variables=variables, definition=definition,
        aux_resources=aux_resources, param_names=param_names,
        policy=policy, sequences=sequences, call_stack=(),
        primary_resource=primary_resource,
        session_resolver=session_resolver,
    )
    plan_steps, estimate = conv.convert(
        recipe.steps,
        defined_steps=set(), defined_vars=set(),
        env_names=set(_ENV_NAMES), branch_depth=0, nested=False,
        path="steps",
    )
    if estimate > MAX_TOTAL_STEPS_ESTIMATE:
        raise SeqExpressionError(
            f"展開後の総ステップ数見積り ({estimate}) が上限 "
            f"({MAX_TOTAL_STEPS_ESTIMATE}) を超えています (spec §10 展開上限)"
        )

    # required_resources: primary + aux を canonical sorted
    req: set[str] = set(aux_resources)
    if primary_resource:
        req.add(primary_resource)
    required = sorted(req)

    return Plan(
        name=(recipe.description[:80] if recipe.description else "recipe"),
        parameters=dict(variables),
        steps=plan_steps,
        resource_hint=primary_resource,
        required_resources=required,
    )


class _StepConverter:
    """RecipeStep 列 → IR Step 列の再帰変換器 (v2.29.0 SP-3 で導入)。

    branch / repeat のネスト steps (raw dict) を RecipeStep として検証しつつ
    再帰変換する。defined_steps / defined_vars は呼び出し側の set を直接
    mutate する (branch case は copy を渡して分岐間の漏れを防ぐ)。
    返り値は (steps, 展開後ステップ数の静的見積り)。
    """

    # ネスト (branch case / repeat body) 内で許可しない step 種別。
    # polling / barrier / pause は Job manager のトップレベル状態遷移
    # (WAITING 等)・pause レコード管理と密結合のため未対応 (docs に記載)。
    _NESTED_FORBIDDEN = frozenset({
        "wait_until", "wait_for_condition", "wait_for_stable", "barrier",
        "pause",
    })

    def __init__(
        self,
        *,
        recipe: RecipeDefinition,
        variables: dict[str, Any],
        definition: InstrumentDefinition | None,
        aux_resources: set[str],
        param_names: set[str],
        policy: "CodePolicy | None" = None,
        sequences: dict[str, Any] | None = None,
        call_stack: tuple[str, ...] = (),
        primary_resource: str | None = None,
        session_resolver: SessionResolver | None = None,
    ):
        self.recipe = recipe
        self.variables = variables
        self.definition = definition
        self.aux_resources = aux_resources
        self.param_names = param_names
        self._policy = policy   # None なら初回使用時に load_policy()
        self.sequences = sequences or {}   # v2.33.0 (SP-7)
        self.call_stack = call_stack       # 再帰検出用
        self.primary_resource = primary_resource
        self.session_resolver = session_resolver

    def _definition_for_instrument(
        self, resource: str | None, context: str, *, strict: bool = False,
    ) -> InstrumentDefinition | None:
        """Resolve the target definition without falling back to the primary."""
        if not resource or resource == self.primary_resource:
            return self.definition
        if self.session_resolver is None:
            # Direct plan conversion cannot inspect another session. Runtime
            # remains the final fail-closed gate in this compatibility mode.
            return self.definition if self.primary_resource is None else None
        target = self.session_resolver(resource)
        if target is None:
            if strict:
                raise SeqExpressionError(
                    f"{context}: InstrumentNotAvailable: target={resource!r}, "
                    f"primary={self.primary_resource!r}"
                )
            return None
        target_definition = getattr(target, "definition", None)
        if target_definition is None:
            if strict:
                raise SeqExpressionError(
                    f"{context}: NoDefinitionFound: target={resource!r}"
                )
            return None
        return target_definition

    @property
    def policy(self) -> "CodePolicy":
        """コード実行ポリシー (py/dll ステップがあるときだけ遅延ロード)。"""
        if self._policy is None:
            from .code_policy import load_policy
            self._policy = load_policy()
        return self._policy

    def convert(
        self,
        raw_steps: list,
        *,
        defined_steps: set[str],
        defined_vars: set[str],
        env_names: set[str],
        branch_depth: int,
        nested: bool,
        path: str,
    ) -> tuple[list[Step], int]:
        out: list[Step] = []
        estimate = 0

        for i, raw in enumerate(raw_steps):
            spath = f"{path}[{i}]"
            rs = self._as_recipe_step(raw, spath)
            st = rs.step_type

            if nested and st in self._NESTED_FORBIDDEN:
                raise SeqExpressionError(
                    f"{spath}: ネストしたシーケンス内の {st} は未対応です"
                )

            if st == "sweep":
                # v2.40.0: 掃引。値の数だけ body を複製する compile 時展開なので、
                # IR にも実行器にも新しい概念を持ち込まない。展開後は
                # 普通のリテラル引数になるため、安全検査も通常の引数と
                # 同じ経路 (safety_validator) で行われる。
                expanded, est = self._convert_sweep(
                    rs, defined_steps, defined_vars, env_names,
                    branch_depth, spath, nested,
                )
                out.extend(expanded)
                estimate += est
                continue

            if st == "compute":
                out.append(self._convert_compute(
                    rs, defined_steps, defined_vars, env_names, spath,
                ))
                defined_vars.add(rs.compute["set"])
                estimate += 1
            elif st == "guard":
                out.append(self._convert_guard(
                    rs, defined_steps, defined_vars, env_names, spath,
                ))
                estimate += 1
            elif st == "pause":
                out.append(self._convert_pause(
                    rs, defined_steps, defined_vars, env_names, spath,
                ))
                estimate += 1
            elif st == "py":
                step = self._convert_py(
                    rs, defined_steps, defined_vars, env_names, spath,
                )
                if nested and step.on_error == "pause":
                    raise SeqExpressionError(
                        f"{spath}: ネストしたシーケンス内の py は on_error=pause を"
                        "指定できません (abort / safe_shutdown を使用)"
                    )
                out.append(step)
                for name in step.outputs:
                    defined_vars.add(name)
                estimate += 1
            elif st == "dll":
                step = self._convert_dll(
                    rs, defined_steps, defined_vars, env_names, spath,
                )
                if nested and step.on_error == "pause":
                    raise SeqExpressionError(
                        f"{spath}: ネストしたシーケンス内の dll は on_error=pause を"
                        "指定できません (abort / safe_shutdown を使用)"
                    )
                out.append(step)
                if step.result_as:
                    defined_steps.add(step.result_as)
                for name in step.out_args.values():
                    defined_vars.add(name)
                estimate += 1
            elif st == "call":
                step, est = self._convert_call(
                    rs, defined_steps, defined_vars, env_names, spath,
                )
                out.append(step)
                # returns_as でマップされた呼び出し元 vars を定義済みに
                for parent_name in step.returns_map.values():
                    defined_vars.add(parent_name)
                estimate += est
            elif st == "branch":
                step, est = self._convert_branch(
                    rs, defined_steps, defined_vars, env_names,
                    branch_depth, spath,
                )
                out.append(step)
                estimate += est
            elif st == "repeat":
                step, est = self._convert_repeat(
                    rs, defined_steps, defined_vars, env_names,
                    branch_depth, spath,
                )
                out.append(step)
                estimate += est
            elif st == "wait":
                seconds_raw = rs.wait["seconds"]
                seconds = float(resolve_arg(seconds_raw, self.variables))
                out.append(WaitStep(seconds=seconds, description=rs.description))
                estimate += 1
            elif st == "wait_until":
                wu = dict(rs.wait_until)
                sec = wu.get("seconds_from_now")
                if isinstance(sec, str):
                    sec = float(resolve_arg(sec, self.variables))
                    wu["seconds_from_now"] = sec
                out.append(WaitUntilStep(
                    timestamp=wu.get("timestamp"),
                    seconds_from_now=wu.get("seconds_from_now"),
                    description=rs.description,
                ))
                estimate += 1
            elif st == "wait_for_condition":
                wfc = dict(rs.wait_for_condition)
                resolved_args = {
                    k: resolve_arg(v, self.variables)
                    for k, v in (wfc.get("args") or {}).items()
                }
                inst = wfc["instrument"]
                self.aux_resources.add(inst)
                out.append(WaitForConditionStep(
                    instrument=inst,
                    command=wfc["command"],
                    args=resolved_args,
                    condition_expr=wfc["condition_expr"],
                    interval_s=float(resolve_arg(wfc.get("interval_s", 1.0), self.variables)),
                    timeout_s=float(resolve_arg(wfc.get("timeout_s", 60.0), self.variables)),
                    command_timeout_s=(
                        float(resolve_arg(wfc["command_timeout_s"], self.variables))
                        if wfc.get("command_timeout_s") is not None else None
                    ),
                    value_path=wfc.get("value_path"),
                    retry_on_error=int(wfc.get("retry_on_error", 1)),
                    max_consecutive_errors=int(wfc.get("max_consecutive_errors", 3)),
                    description=rs.description,
                ))
                estimate += 1
            elif st == "wait_for_stable":
                wfs = dict(rs.wait_for_stable)
                resolved_args = {
                    k: resolve_arg(v, self.variables)
                    for k, v in (wfs.get("args") or {}).items()
                }
                inst = wfs["instrument"]
                self.aux_resources.add(inst)
                out.append(WaitForStableStep(
                    instrument=inst,
                    command=wfs["command"],
                    args=resolved_args,
                    tolerance=float(resolve_arg(wfs["tolerance"], self.variables)),
                    window_s=float(resolve_arg(wfs["window_s"], self.variables)),
                    interval_s=float(resolve_arg(wfs.get("interval_s", 1.0), self.variables)),
                    timeout_s=float(resolve_arg(wfs.get("timeout_s", 60.0), self.variables)),
                    command_timeout_s=(
                        float(resolve_arg(wfs["command_timeout_s"], self.variables))
                        if wfs.get("command_timeout_s") is not None else None
                    ),
                    value_path=wfs.get("value_path"),
                    min_samples=int(wfs.get("min_samples", 3)),
                    method=wfs.get("method", "range"),
                    retry_on_error=int(wfs.get("retry_on_error", 1)),
                    max_consecutive_errors=int(wfs.get("max_consecutive_errors", 3)),
                    description=rs.description,
                ))
                estimate += 1
            elif st == "barrier":
                br = dict(rs.barrier)
                out.append(BarrierStep(
                    name=br["name"],
                    timeout_s=float(resolve_arg(br.get("timeout_s", 60.0), self.variables)),
                    description=rs.description,
                ))
                estimate += 1
            else:  # command
                out.append(self._convert_command(
                    rs, defined_steps, defined_vars, env_names, spath,
                ))
                if rs.result_as:
                    defined_steps.add(rs.result_as)
                estimate += 1

        return out, estimate

    # ---- 個別変換 ----

    @staticmethod
    def _as_recipe_step(raw, spath: str) -> RecipeStep:
        if isinstance(raw, RecipeStep):
            return raw
        try:
            return RecipeStep.model_validate(raw)
        except Exception as e:  # pydantic ValidationError 等
            raise SeqExpressionError(f"{spath}: ステップ定義が不正です: {e}")

    def _convert_compute(
        self, rs: RecipeStep,
        defined_steps: set[str], defined_vars: set[str],
        env_names: set[str], spath: str,
    ) -> ComputeStep:
        cp = rs.compute or {}
        set_name = cp["set"]
        expr = cp["expr"]
        _validate_expr_refs(
            expr,
            defined_steps=defined_steps, defined_vars=defined_vars,
            param_names=self.param_names, env_names=env_names,
            context=f"{spath} compute(set={set_name})",
        )
        return ComputeStep(
            set=set_name,
            expr=expr,
            unit=cp.get("unit", ""),
            on_error=cp.get("on_error", "abort"),
            description=rs.description,
        )

    def _convert_guard(
        self, rs: RecipeStep,
        defined_steps: set[str], defined_vars: set[str],
        env_names: set[str], spath: str,
    ) -> GuardStep:
        gd = rs.guard or {}
        _validate_expr_refs(
            gd["expr"],
            defined_steps=defined_steps, defined_vars=defined_vars,
            param_names=self.param_names, env_names=env_names,
            context=f"{spath} guard",
        )
        return GuardStep(
            expr=gd["expr"],
            on_fail=gd.get("on_fail", "abort"),
            message=gd.get("message", ""),
            description=rs.description,
        )

    def _convert_pause(
        self, rs: RecipeStep,
        defined_steps: set[str], defined_vars: set[str],
        env_names: set[str], spath: str,
    ) -> PauseStep:
        """pause の変換 (v2.30.0 SP-4)。

        - message 内の ${...} (表示文字列補間) をコンパイル時検証
        - expose の各参照式をコンパイル時検証
        - timeout_s は $param 解決可
        """
        ps = rs.pause or {}
        message = str(ps.get("message", ""))
        for expr in string_expr_parts(message):
            _validate_expr_refs(
                expr,
                defined_steps=defined_steps, defined_vars=defined_vars,
                param_names=self.param_names, env_names=env_names,
                context=f"{spath} pause.message",
            )
        expose = [str(x) for x in (ps.get("expose") or [])]
        for expr in expose:
            _validate_expr_refs(
                expr,
                defined_steps=defined_steps, defined_vars=defined_vars,
                param_names=self.param_names, env_names=env_names,
                context=f"{spath} pause.expose",
            )
        try:
            timeout_s = float(resolve_arg(ps.get("timeout_s", 3600.0), self.variables))
        except (ExpressionError, TypeError, ValueError) as e:
            raise SeqExpressionError(f"{spath}: pause.timeout_s を解決できません: {e}")
        return PauseStep(
            message=message,
            timeout_s=timeout_s,
            on_timeout=ps.get("on_timeout", "safe_shutdown"),
            expose=expose,
            description=rs.description,
        )

    def _convert_py(
        self, rs: RecipeStep,
        defined_steps: set[str], defined_vars: set[str],
        env_names: set[str], spath: str,
    ) -> PyStep:
        """py の変換 (v2.32.0 SP-6): path 解決 + sha256 + ポリシーゲート。"""
        from pathlib import Path as _Path

        from .code_policy import (
            CodePolicyError, check_python, resolve_py_file,
            sha256_file, sha256_text,
        )
        from .experiment_ir.context import VariableStoreError, validate_var_name

        ps = rs.py or {}
        # inputs の参照式をコンパイル時検証
        inputs = {str(k): str(v) for k, v in (ps.get("inputs") or {}).items()}
        for local, expr in inputs.items():
            _validate_expr_refs(
                expr,
                defined_steps=defined_steps, defined_vars=defined_vars,
                param_names=self.param_names, env_names=env_names,
                context=f"{spath} py.inputs[{local}]",
            )
        outputs = [str(x) for x in (ps.get("outputs") or [])]
        for name in outputs:
            try:
                validate_var_name(name)
            except VariableStoreError as e:
                raise SeqExpressionError(f"{spath}: py.outputs が不正です: {e}")

        file_ref = ps.get("file")
        code = ps.get("code")
        resolved_path = ""
        try:
            if file_ref:
                p = resolve_py_file(self.policy, str(file_ref))
                resolved_path = str(p)
                digest = sha256_file(p)
                check_python(self.policy, file_path=p, sha256=digest)
            else:
                digest = sha256_text(str(code))
                check_python(self.policy, file_path=None, sha256=digest)
        except CodePolicyError as e:
            raise SeqExpressionError(f"{spath}: {e}")

        try:
            timeout_s = float(ps.get("timeout_s", 60.0))
        except (TypeError, ValueError) as e:
            raise SeqExpressionError(f"{spath}: py.timeout_s が不正です: {e}")

        policy_dir = (
            str(_Path(self.policy.source).parent)
            if self.policy.source != "default" else ""
        )
        return PyStep(
            file=str(file_ref) if file_ref else None,
            code=str(code) if code is not None else None,
            resolved_path=resolved_path,
            sha256=digest,
            policy_dir=policy_dir,
            inputs=inputs,
            outputs=outputs,
            timeout_s=timeout_s,
            on_error=ps.get("on_error", "abort"),
            description=rs.description,
        )

    def _convert_dll(
        self, rs: RecipeStep,
        defined_steps: set[str], defined_vars: set[str],
        env_names: set[str], spath: str,
    ) -> DllStep:
        """dll の変換 (v2.32.0 SP-6): path 検証 + sha256 + ポリシーゲート。"""
        from .code_policy import CodePolicyError, check_dll, sha256_file
        from .experiment_ir.context import VariableStoreError, validate_var_name

        dl = rs.dll or {}
        from pathlib import Path as _Path
        dll_path = _Path(str(dl["path"])).resolve()
        if not dll_path.exists():
            raise SeqExpressionError(f"{spath}: dll.path が存在しません: {dll_path}")
        digest = sha256_file(dll_path)
        try:
            check_dll(self.policy, path=dll_path, sha256=digest)
        except CodePolicyError as e:
            raise SeqExpressionError(f"{spath}: {e}")

        # args: 数値はそのまま、文字列は参照式 (${...} / 裸のどちらも可) として検証
        raw_args = list(dl.get("args") or [])
        norm_args: list[Any] = []
        for i, a in enumerate(raw_args):
            if isinstance(a, str):
                expr = parse_deferred(a)
                if expr is None:
                    expr = a
                _validate_expr_refs(
                    expr,
                    defined_steps=defined_steps, defined_vars=defined_vars,
                    param_names=self.param_names, env_names=env_names,
                    context=f"{spath} dll.args[{i}]",
                )
                norm_args.append(expr)   # 実行時に evaluate される式として保持
            else:
                norm_args.append(a)

        out_args: dict[str, str] = {}
        for k, v in (dl.get("out_args") or {}).items():
            try:
                idx = int(k)
            except (TypeError, ValueError):
                raise SeqExpressionError(
                    f"{spath}: dll.out_args のキーは引数位置 (整数) が必要です: {k!r}"
                )
            if not (0 <= idx < len(raw_args)):
                raise SeqExpressionError(
                    f"{spath}: dll.out_args のインデックス {idx} が args の範囲外です"
                )
            try:
                validate_var_name(str(v))
            except VariableStoreError as e:
                raise SeqExpressionError(f"{spath}: dll.out_args が不正です: {e}")
            out_args[str(idx)] = str(v)

        result_as = dl.get("result_as")
        if result_as:
            try:
                validate_var_name(str(result_as))
            except VariableStoreError as e:
                raise SeqExpressionError(f"{spath}: dll.result_as が不正です: {e}")

        try:
            timeout_s = float(dl.get("timeout_s", 30.0))
        except (TypeError, ValueError) as e:
            raise SeqExpressionError(f"{spath}: dll.timeout_s が不正です: {e}")

        policy_dir = (
            str(_Path(self.policy.source).parent)
            if self.policy.source != "default" else ""
        )
        return DllStep(
            path=str(dll_path),
            function=str(dl["function"]),
            argtypes=[str(t) for t in (dl.get("argtypes") or [])],
            restype=str(dl.get("restype") or "void"),
            args=norm_args,
            out_args=out_args,
            result_as=str(result_as) if result_as else None,
            sha256=digest,
            policy_dir=policy_dir,
            timeout_s=timeout_s,
            on_error=dl.get("on_error", "abort"),
            description=rs.description,
        )

    @staticmethod
    def _replace_roles(raw: Any, bind: dict[str, str], spath: str) -> Any:
        """v2.33.0 (SP-7): ステップ (raw dict) 内の instrument: "@role" を
        bind 先リソースに再帰的に置換する (branch case / repeat body も辿る)。
        """
        if isinstance(raw, dict):
            new: dict[str, Any] = {}
            for k, v in raw.items():
                if k == "instrument" and isinstance(v, str) and v.startswith("@"):
                    role = v[1:]
                    if role not in bind:
                        raise SeqExpressionError(
                            f"{spath}: ロール @{role} が call.bind で束縛されていません"
                        )
                    new[k] = bind[role]
                else:
                    new[k] = _StepConverter._replace_roles(v, bind, spath)
            return new
        if isinstance(raw, list):
            return [_StepConverter._replace_roles(x, bind, spath) for x in raw]
        return raw

    def _convert_call(
        self, rs: RecipeStep,
        defined_steps: set[str], defined_vars: set[str],
        env_names: set[str], spath: str,
    ) -> tuple[CallStep, int]:
        """v2.33.0 (SP-7): call をサブシーケンスの展開済み IR に変換する。"""
        from .asset.capability import match_capabilities

        cl = rs.call or {}
        name = str(cl["sequence"])
        # --- 解決 ---
        entry = self.sequences.get(name)
        if entry is None:
            raise SeqExpressionError(
                f"{spath}: サブシーケンス '{name}' が見つかりません "
                f"(利用可能: {sorted(self.sequences)})"
            )
        seq = entry["seq"]
        lib_sha256 = entry.get("sha256", "")
        # --- 再帰・深さ ---
        if name in self.call_stack:
            raise SeqExpressionError(
                f"{spath}: サブシーケンスの再帰呼び出しは禁止です "
                f"(呼び出し経路: {' -> '.join(self.call_stack + (name,))})"
            )
        if len(self.call_stack) + 1 > CALL_MAX_DEPTH:
            raise SeqExpressionError(
                f"{spath}: call のネスト深さが上限 ({CALL_MAX_DEPTH}) を超えています"
            )
        # --- bind とロール capability 照合 ---
        bind = {str(k): str(v) for k, v in (cl.get("bind") or {}).items()}
        for role in seq.roles:
            if role.name not in bind:
                raise SeqExpressionError(
                    f"{spath}: ロール '{role.name}' が call.bind で束縛されていません"
                )
            if role.requires is not None:
                target_definition = self._definition_for_instrument(
                    bind[role.name], f"{spath}: role '{role.name}'",
                    strict=self.session_resolver is not None,
                )
                if target_definition is not None:
                    m = match_capabilities(role.requires, target_definition)
                    if not m.get("satisfied", False):
                        raise SeqExpressionError(
                            f"{spath}: ロール '{role.name}' の capability 要件を "
                            f"bind 先装置が満たしません: "
                            f"missing={m.get('missing_commands')} "
                            f"ranges={m.get('range_violations')}"
                        )
        # --- with の解決 ---
        # コンパイル時解決値 (定数 / 呼び出し元 $param) は sub_params へ →
        #   サブ内で $X (コンパイル時) と params.X (実行時) の両方で参照可。
        # 実行時式 ${...} は with_exprs へ (SP-7.1、v2.34.0) → 呼び出し元スコープで
        #   process_call_step が評価。サブ内では params.X のみ参照可 ($X は不可)。
        sub_params: dict[str, Any] = {}
        with_exprs: dict[str, str] = {}
        runtime_param_names: set[str] = set()
        for pname, praw in (cl.get("with") or {}).items():
            pname = str(pname)
            expr = parse_deferred(praw)  # ${...} 全体なら inner 式、無ければ None
            if expr is not None:
                # 実行時式: 呼び出し元スコープ (親の defined) で参照を検証
                _validate_expr_refs(
                    expr,
                    defined_steps=defined_steps, defined_vars=defined_vars,
                    param_names=self.param_names, env_names=env_names,
                    context=f"{spath}: call.with['{pname}'] (${{...}})",
                )
                with_exprs[pname] = expr
                runtime_param_names.add(pname)
            else:
                try:
                    sub_params[pname] = resolve_arg(praw, self.variables)
                except (ExpressionError, TypeError, ValueError) as e:
                    raise SeqExpressionError(
                        f"{spath}: call.with['{pname}'] を解決できません: {e}"
                    )
        # 宣言済み parameters の default を補完 (with で与えられていない分のみ)
        for p in seq.parameters:
            if (p.name not in sub_params and p.name not in with_exprs
                    and p.default is not None):
                sub_params[p.name] = p.default
        # --- returns_as 検証 ---
        returns_as = {str(k): str(v) for k, v in (cl.get("returns_as") or {}).items()}
        for sub_name in returns_as:
            if sub_name not in seq.returns:
                raise SeqExpressionError(
                    f"{spath}: call.returns_as の '{sub_name}' は "
                    f"サブシーケンスの returns {seq.returns} に含まれません"
                )
        # --- サブステップの @role 置換 + 変換 (子スコープの新 converter) ---
        raw_steps = [self._replace_roles(s.model_dump(), bind, spath)
                     for s in seq.steps]
        sub_recipe = RecipeDefinition(
            description=seq.description, parameters=seq.parameters,
            # callはinline expansionであり、親recipeの安全制約を弱めてはならない。
            # requires.rangesを子commandにも適用し、装置rangeとの積集合を維持する。
            steps=[], requires=self.recipe.requires,
            on_error=self.recipe.on_error,
        )
        # param_names: サブ内で params.X 参照が有効な名前。
        # 実行時 with (runtime_param_names) も params.X としては参照可なので含める。
        # ただし variables (=sub_params、$X コンパイル時解決の元) には含めない →
        # 実行時 with を $X で使うとコンパイルエラーになる (意図した制約)。
        sub_param_names = (
            set(sub_params.keys()) | runtime_param_names
            | {p.name for p in seq.parameters}
        )
        sub_conv = _StepConverter(
            recipe=sub_recipe, variables=sub_params, definition=self.definition,
            aux_resources=self.aux_resources, param_names=sub_param_names,
            policy=self._policy, sequences=self.sequences,
            call_stack=self.call_stack + (name,),
            primary_resource=self.primary_resource,
            session_resolver=self.session_resolver,
        )
        sub_steps, est = sub_conv.convert(
            raw_steps,
            defined_steps=set(), defined_vars=set(),
            env_names=set(env_names), branch_depth=0, nested=True,
            path=f"{spath}/call:{name}/steps",
        )
        # returns_as の子側名が展開後に定義されているか (steps/vars) の確認は
        # process_call_step が実行時に行う (未定義は返り値エラー)。
        step = CallStep(
            sequence=name, sub_steps=sub_steps, sub_params=sub_params,
            with_exprs=with_exprs,
            returns_map=returns_as, lib_sha256=lib_sha256,
            description=rs.description or seq.description,
        )
        return step, est + 1

    def _convert_branch(
        self, rs: RecipeStep,
        defined_steps: set[str], defined_vars: set[str],
        env_names: set[str], branch_depth: int, spath: str,
    ) -> tuple[BranchStep, int]:
        if branch_depth + 1 > BRANCH_MAX_DEPTH:
            raise SeqExpressionError(
                f"{spath}: branch のネスト深さが上限 ({BRANCH_MAX_DEPTH}) を超えています"
            )
        cases: list[BranchCase] = []
        new_defs: list[tuple[set[str], set[str]]] = []
        case_estimates: list[int] = []
        has_else = False

        for ci, case in enumerate(rs.branch or []):
            is_else = "else" in case
            when = None if is_else else case.get("when")
            if is_else:
                has_else = True
            if when is not None:
                _validate_expr_refs(
                    when,
                    defined_steps=defined_steps, defined_vars=defined_vars,
                    param_names=self.param_names, env_names=env_names,
                    context=f"{spath}/branch case[{ci}].when",
                )
            # 各 case は defined set の copy を使う (分岐間の定義漏れ防止)
            ds = set(defined_steps)
            dv = set(defined_vars)
            sub_steps, est = self.convert(
                case["steps"],
                defined_steps=ds, defined_vars=dv, env_names=env_names,
                branch_depth=branch_depth + 1, nested=True,
                path=f"{spath}/case[{ci}]/steps",
            )
            cases.append(BranchCase(when=when, steps=sub_steps))
            new_defs.append((ds - defined_steps, dv - defined_vars))
            case_estimates.append(est)

        # 全経路定義検証 (spec §3): else がある場合のみ、**全 case で定義された**
        # 名前を分岐後スコープへ伝播する。else が無い場合は「どの case も実行され
        # ない」経路があるため何も伝播しない → 分岐後にその変数を参照すると
        # 未定義参照としてコンパイルエラーになる。
        if has_else and new_defs:
            common_steps = set.intersection(*[d[0] for d in new_defs])
            common_vars = set.intersection(*[d[1] for d in new_defs])
            defined_steps |= common_steps
            defined_vars |= common_vars

        step = BranchStep(cases=cases, description=rs.description)
        return step, 1 + (max(case_estimates) if case_estimates else 0)

    def _convert_sweep(
        self, rs, defined_steps, defined_vars, env_names,
        branch_depth, spath, nested,
    ):
        """掃引を、値の数だけ複製した平らなステップ列に展開する。

        反復 (repeat) との違いは解決の時点にある。掃引の値は
        **コンパイル時に確定する**ので、body の中では通常のパラメータ
        (``$name``) として参照でき、展開後は普通のリテラル引数になる。
        実行時に決まる ``${...}`` のように範囲宣言を要求しないのは、
        値が実行前に確定していて dry-run でそのまま読めるからである
        (検査そのものは通常の引数と同じ safety_validator が行う)。

        戻り値は (展開済みステップ列, 静的見積り)。
        """
        from lab_executor.dsl.schema import MAX_SWEEP_POINTS, SweepValues

        sw = rs.sweep or {}
        parameter = sw.get("parameter")
        if not isinstance(parameter, str) or not parameter:
            raise SeqExpressionError(f"{spath}: sweep には parameter が必要です")
        if parameter in ("params", "steps", "vars", "env", "value", "np"):
            raise SeqExpressionError(
                f"{spath}: sweep.parameter に予約語は使えません: {parameter}"
            )
        raw_steps = sw.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps:
            raise SeqExpressionError(f"{spath}: sweep には steps が必要です")

        try:
            values = SweepValues(**(sw.get("values") or {})).expand()
        except Exception as exc:  # noqa: BLE001 - 検証理由をそのまま伝える
            raise SeqExpressionError(f"{spath}: sweep.values が不正です: {exc}")
        if len(values) > MAX_SWEEP_POINTS:
            raise SeqExpressionError(
                f"{spath}: sweep の展開点数 {len(values)} が上限 "
                f"{MAX_SWEEP_POINTS} を超えています"
            )

        # 掃引変数は body の中だけで有効。同名のパラメータがあれば退避して戻す。
        had_previous = parameter in self.variables
        previous = self.variables.get(parameter)

        expanded: list = []
        estimate = 0
        try:
            for index, value in enumerate(values):
                self.variables[parameter] = value
                steps, est = self.convert(
                    raw_steps,
                    defined_steps=defined_steps,
                    defined_vars=defined_vars,
                    env_names=env_names,
                    branch_depth=branch_depth,
                    nested=nested,
                    path=f"{spath}/sweep[{index}]",
                )
                expanded.extend(steps)
                estimate += est
        finally:
            if had_previous:
                self.variables[parameter] = previous
            else:
                self.variables.pop(parameter, None)

        if estimate > MAX_TOTAL_STEPS_ESTIMATE:
            raise SeqExpressionError(
                f"{spath}: sweep 展開後のステップ数 {estimate} が上限 "
                f"{MAX_TOTAL_STEPS_ESTIMATE} を超えています"
            )
        return expanded, estimate

    def _convert_repeat(
        self, rs: RecipeStep,
        defined_steps: set[str], defined_vars: set[str],
        env_names: set[str], branch_depth: int, spath: str,
    ) -> tuple[RepeatStep, int]:
        rp = rs.repeat or {}
        body_env = set(env_names) | {"loop_index"}
        collect: dict[str, str] = {
            str(k): str(v) for k, v in (rp.get("collect") or {}).items()
        }

        def _exact_int(value: Any, field: str) -> int:
            """Accept integral numeric values without silently truncating."""
            import math

            if isinstance(value, bool):
                raise SeqExpressionError(f"{spath}: {field} は整数が必要です: {value!r}")
            try:
                number = float(value)
            except (TypeError, ValueError) as e:
                raise SeqExpressionError(
                    f"{spath}: {field} は整数が必要です: {value!r}"
                ) from e
            if not math.isfinite(number) or not number.is_integer():
                raise SeqExpressionError(
                    f"{spath}: {field} は整数が必要です: {value!r}"
                )
            return int(number)

        def _validate_collect(body_new_steps: set[str], body_new_vars: set[str]) -> None:
            """collect の検証 (SP-5): source は body 内で定義される名前、
            target は命名規則を満たす array 変数名。"""
            from .experiment_ir.context import VariableStoreError, validate_var_name
            body_defined = body_new_steps | body_new_vars
            for src, target in collect.items():
                if src not in body_defined:
                    raise SeqExpressionError(
                        f"{spath}: repeat.collect の '{src}' は repeat body 内で"
                        "定義されません (result_as / compute set が必要)"
                    )
                try:
                    validate_var_name(target)
                except VariableStoreError as e:
                    raise SeqExpressionError(
                        f"{spath}: repeat.collect の array 変数名が不正です: {e}"
                    )

        if rp.get("count") is not None:
            # count はコンパイル時解決 ($param 可)
            try:
                resolved_count = resolve_arg(rp["count"], self.variables)
                count = _exact_int(resolved_count, "repeat.count")
            except (ExpressionError, TypeError, ValueError) as e:
                raise SeqExpressionError(f"{spath}: repeat.count を解決できません: {e}")
            if count < 1:
                raise SeqExpressionError(
                    f"{spath}: repeat.count は 1 以上である必要があります: {count}"
                )
            if count > REPEAT_MAX_COUNT:
                raise SeqExpressionError(
                    f"{spath}: repeat.count ({count}) が上限 ({REPEAT_MAX_COUNT}) を"
                    "超えています"
                )
            # count >= 1 が保証されるため body 内の定義は分岐後スコープへ伝播する
            pre_steps = set(defined_steps)
            pre_vars = set(defined_vars)
            body_steps, est = self.convert(
                rp["steps"],
                defined_steps=defined_steps, defined_vars=defined_vars,
                env_names=body_env,
                branch_depth=branch_depth, nested=True,
                path=f"{spath}/repeat/steps",
            )
            _validate_collect(
                defined_steps - pre_steps, defined_vars - pre_vars,
            )
            # collect の array 変数は repeat 終了時に必ず定義される
            defined_vars.update(collect.values())
            step = RepeatStep(
                count=count, body=body_steps, collect=collect,
                description=rs.description,
            )
            return step, 1 + count * est

        # while 型
        while_expr = rp["while"]
        max_it = _exact_int(rp["max_iterations"], "repeat.max_iterations")
        if max_it < 1:
            raise SeqExpressionError(
                f"{spath}: repeat.max_iterations は 1 以上である必要があります: {max_it}"
            )
        if max_it > REPEAT_MAX_COUNT:
            raise SeqExpressionError(
                f"{spath}: repeat.max_iterations ({max_it}) が上限 "
                f"({REPEAT_MAX_COUNT}) を超えています"
            )
        # while 条件は最初の反復の**前**に評価されるため、body 内でのみ定義される
        # 変数は参照できない (外側の定義のみで検証。loop_index も不可)
        _validate_expr_refs(
            while_expr,
            defined_steps=defined_steps, defined_vars=defined_vars,
            param_names=self.param_names, env_names=env_names,
            context=f"{spath}/repeat.while",
        )
        # while は 0 回実行があり得るため body 内の定義は伝播しない (copy で検証)
        ds = set(defined_steps)
        dv = set(defined_vars)
        body_steps, est = self.convert(
            rp["steps"],
            defined_steps=ds, defined_vars=dv, env_names=body_env,
            branch_depth=branch_depth, nested=True,
            path=f"{spath}/repeat/steps",
        )
        _validate_collect(ds - defined_steps, dv - defined_vars)
        # collect の array は 0 回実行でも空配列として定義される (spec §3/§5.4)
        defined_vars.update(collect.values())
        step = RepeatStep(
            while_expr=while_expr, max_iterations=max_it,
            body=body_steps, collect=collect, description=rs.description,
        )
        return step, 1 + max_it * est

    def _convert_command(
        self, rs: RecipeStep,
        defined_steps: set[str], defined_vars: set[str],
        env_names: set[str], spath: str,
    ) -> CommandStep:
        instrument = getattr(rs, "instrument", None)
        if instrument and instrument != self.primary_resource:
            self.aux_resources.add(instrument)
        target_definition = self._definition_for_instrument(instrument, spath)
        resolved_args: dict[str, Any] = {}
        deferred_args: dict[str, Any] = {}
        for k, v in rs.args.items():
            expr = parse_deferred(v)  # ${...} 検出 (SP-2)
            if expr is not None:
                _validate_expr_refs(
                    expr,
                    defined_steps=defined_steps, defined_vars=defined_vars,
                    param_names=self.param_names, env_names=env_names,
                    context=f"{spath} {rs.command}.{k} (${{...}})",
                )
                mn, mx = _resolve_range_decl(
                    self.recipe, target_definition, rs.command or "", k,
                )
                deferred_args[k] = {"expr": expr, "min": mn, "max": mx}
            else:
                resolved_args[k] = resolve_arg(v, self.variables)
        # v0.6.0: instrument は logical ref ($psu / alias / resource_name) としてそのまま渡す。
        # 実 resource への解決は Job executor / step_executor 側で行う。
        return CommandStep(
            command=rs.command or "",
            args=resolved_args,
            deferred_args=deferred_args,
            result_as=rs.result_as,
            value_path=rs.value_path or "",
            unit=rs.unit or "",
            description=rs.description,
            instrument=instrument,
            stagger_ms=getattr(rs, "stagger_ms", None),
            on_error=rs.on_error if rs.on_error is not None else self.recipe.on_error,
        )


# ============================================================
# Plan executor (各 Step type を dispatch)
# ============================================================

async def execute_plan(
    visa: VisaManager,
    session: InstrumentSession,
    plan: Plan,
    recipe_name: str | None = None,
    override_safety: bool = False,
    override_reason: str = "",
    session_resolver: SessionResolver | None = None,
) -> dict:
    """
    IR Plan を実行する。返り値の形式は execute_recipe と同じ (後方互換)。
    """
    if session.definition is None:
        return {
            "success": False,
            "recipe": recipe_name or plan.name,
            "error": "NoDefinitionFound",
            "message": "機器定義が読み込まれていません",
            "steps_executed": [],
        }

    step_results: list[dict] = []
    resolve_session = session_resolver or seq_runtime._primary_only_resolver(session)
    written_resources: set[str] = set()

    def _record_write(resource: str) -> None:
        written_resources.add(resource)

    # v0.5.1.1 / v0.6.1: polling / barrier 系 step は同期 execute_recipe では実行不可。
    # LLM が誤って execute_recipe を選んだ場合に分かりやすく Job 化を促す。
    # barrier は Map Job 内の target 間同期なので、単一 target の execute_recipe では
    # 永遠に成立しない (1 つの target だけが arrive して全 target 揃わない)。
    # v2.30.0 (SP-4): pause も同様 (応答待ちの pause レコード管理は Job 経路のみ)。
    # v2.32.0 (SP-6): py / dll の on_error=pause も pause 機構が必要なため Job 経路のみ。
    for s in plan.steps:
        if (
            isinstance(s, (WaitUntilStep, WaitForConditionStep, WaitForStableStep,
                           BarrierStep, PauseStep))
            or (isinstance(s, (PyStep, DllStep)) and s.on_error == "pause")
        ):
            is_barrier = isinstance(s, BarrierStep)
            return {
                "success": False,
                "recipe": recipe_name or plan.name,
                "error": "AsyncStepRequiresJob",
                "message": (
                    "wait_until / wait_for_condition / wait_for_stable / barrier / pause "
                    "を含む recipe は execute_recipe では実行できません。"
                    + ("**start_map_recipe_job** を使ってください "
                       "(barrier は target 間同期のため Map Job で意味を持ちます)。"
                       if is_barrier else
                       "**start_recipe_job** を使ってください。")
                    + " (進捗は get_job_status、完了結果は get_job_result で取得)"
                ),
                "async_step_type": getattr(s, "type", "?"),
                "recommended_action": {
                    "tool": "start_map_recipe_job" if is_barrier else "start_recipe_job",
                    "args": (
                        {"recipe": recipe_name or plan.name, "targets": "<target list>"}
                        if is_barrier else
                        {"resource_name": "<同じ resource>",
                         "recipe_name": recipe_name or plan.name}
                    ),
                },
                "steps_executed": [],
            }

    # v2.28.0 (SP-1/2): 変数ストア (params / env)。同期経路では job_id は "" とする。
    import datetime as _dt
    store = VariableStore(
        params=dict(plan.parameters),
        env={"job_id": "", "started_at": _dt.datetime.now().isoformat()},
    )

    async def _safe_shutdown() -> dict:
        resources: dict[str, dict] = {}
        all_ok = True
        # Preserve the established single-device behavior for recipes that
        # request safe shutdown before their first write. Cross-device runs use
        # the precise set of write-attempted resources.
        legacy_single = set(plan.required_resources or [session.resource_name]) <= {
            session.resource_name,
        }
        shutdown_resources = (
            written_resources
            or ({session.resource_name} if legacy_single else set())
        )
        for resource in sorted(shutdown_resources):
            try:
                target = resolve_session(resource)
                if target is None:
                    result = {
                        "attempted": False, "success": False,
                        "error": "InstrumentNotAvailable",
                    }
                elif target.definition is None:
                    result = {
                        "attempted": False, "success": False,
                        "error": "NoDefinitionFound",
                    }
                else:
                    result = await _run_safe_shutdown_sync(visa, target)
            except Exception as e:  # noqa: BLE001
                result = {
                    "attempted": True, "success": False,
                    "error": type(e).__name__, "message": str(e),
                }
            resources[resource] = result
            all_ok = all_ok and bool(result.get("success"))
        if list(resources) == [session.resource_name]:
            # Preserve the public single-instrument shutdown schema.
            return resources[session.resource_name]
        return {
            "attempted": bool(shutdown_resources),
            "success": all_ok,
            "resources": resources,
        }

    # v2.29.0 (SP-3): branch / repeat 内のリーフ step 実行コールバック (同期経路)
    async def _nested_run_command(st: CommandStep, path: str, active_store) -> dict:
        return await seq_runtime.process_command_step(
            visa, session, st, active_store,
            override_safety=override_safety,
            override_reason=override_reason,
            source_step_path=path,
            safe_shutdown=_safe_shutdown,
            session_resolver=resolve_session,
            record_write=_record_write,
        )

    async def _nested_run_wait(st: WaitStep, path: str) -> dict:
        return await execute_wait_step(st)

    nested_execs = seq_runtime.NestedExecutors(
        run_command=_nested_run_command,
        run_wait=_nested_run_wait,
        cancel_check=None,
    )

    for idx, step in enumerate(plan.steps):
        if isinstance(step, WaitStep):
            result = await execute_wait_step(step)
        elif isinstance(step, ComputeStep):
            result = await seq_runtime.process_compute_step(
                step, store,
                source_step_path=f"steps[{idx}]",
                safe_shutdown=_safe_shutdown,
            )
        elif isinstance(step, GuardStep):
            result = await seq_runtime.process_guard_step(
                step, store,
                source_step_path=f"steps[{idx}]",
                safe_shutdown=_safe_shutdown,
            )
        elif isinstance(step, BranchStep):
            result = await seq_runtime.process_branch_step(
                step, store, nested_execs,
                source_step_path=f"steps[{idx}]",
                safe_shutdown=_safe_shutdown,
            )
        elif isinstance(step, RepeatStep):
            result = await seq_runtime.process_repeat_step(
                step, store, nested_execs,
                source_step_path=f"steps[{idx}]",
                safe_shutdown=_safe_shutdown,
            )
        elif isinstance(step, CallStep):
            result = await seq_runtime.process_call_step(
                step, store, nested_execs,
                source_step_path=f"steps[{idx}]",
                safe_shutdown=_safe_shutdown,
            )
        elif isinstance(step, PyStep):
            result = await seq_runtime.process_py_step(
                step, store,
                source_step_path=f"steps[{idx}]",
                safe_shutdown=_safe_shutdown,
            )
        elif isinstance(step, DllStep):
            result = await seq_runtime.process_dll_step(
                step, store,
                source_step_path=f"steps[{idx}]",
                safe_shutdown=_safe_shutdown,
            )
        elif isinstance(step, CommandStep):
            result = await seq_runtime.process_command_step(
                visa, session, step, store,
                override_safety=override_safety,
                override_reason=override_reason,
                source_step_path=f"steps[{idx}]",
                safe_shutdown=_safe_shutdown,
                session_resolver=resolve_session,
                record_write=_record_write,
            )
        else:
            # 将来 step type 追加時に備えた fallback
            result = {
                "success": False,
                "error": "UnsupportedStepType",
                "step_type": getattr(step, "type", "unknown"),
                "message": "未対応のステップ型です",
            }

        step_results.append({"step": idx, **result})

        if not result.get("success", False):
            return {
                "success": False,
                "recipe": recipe_name or plan.name,
                "steps_executed": step_results,
                "halted_at_step": idx,
                "variables": store.snapshot(),
            }

    return {
        "success": True,
        "recipe": recipe_name or plan.name,
        "steps_executed": step_results,
        "step_count": len(step_results),
        "variables": store.snapshot(),
    }


async def _run_safe_shutdown_sync(visa: VisaManager, session: InstrumentSession) -> dict:
    """同期経路 (execute_plan) 用の簡易 safe_shutdown。

    機器定義の ``safe_shutdown`` に定義された command ステップを override_safety=True
    で順次実行する (wait は同期経路ではスキップ)。Job 経路は manager 側の
    ``_best_effort_safe_shutdown`` を使う。
    """
    steps = list(getattr(session.definition, "safe_shutdown", None) or [])
    results: list[dict] = []
    all_ok = True
    for idx, rs in enumerate(steps):
        if getattr(rs, "step_type", "command") != "command":
            continue
        try:
            cs = CommandStep(command=rs.command or "", args=rs.args)
            r = await execute_command_step(
                visa, session, cs,
                override_safety=True, override_reason="safe_shutdown (sync)",
            )
            ok = bool(r.get("success"))
            all_ok = all_ok and ok
            results.append({"step": idx, "command": rs.command, "success": ok})
        except Exception as e:  # noqa: BLE001
            all_ok = False
            results.append({
                "step": idx, "command": rs.command,
                "success": False, "error": type(e).__name__,
            })
    return {
        "attempted": bool(steps),
        "source": "yaml" if steps else "none",
        "success": all_ok,
        "steps": results,
    }


# ============================================================
# 公開エントリポイント (既存 API、後方互換維持)
# ============================================================
# 個別 step 実行ロジックは v0.5.0.1 で step_executor.py に切り出し済み。
# このモジュールは Recipe 単位の orchestration のみを担当する。

async def execute_recipe(
    visa: VisaManager,
    session: InstrumentSession,
    recipe_name: str,
    parameters: dict[str, Any] | None,
    override_safety: bool = False,
    override_reason: str = "",
    session_resolver: SessionResolver | None = None,
    recipe_library: Any = None,
) -> dict:
    """
    指定の recipe を実行する。

    v0.5.0-rc1 で内部実装を IR Plan ベースに refactor したが、戻り値形式は v0.3.0/v0.4.x と同一。
    """
    parameters = parameters or {}

    if session.definition is None:
        return {
            "success": False,
            "error": "NoDefinitionFound",
            "message": "機器定義が読み込まれていません",
        }

    # v2.39.0: 定義のレシピを優先し、無ければ利用者のライブラリを見る。
    from lab_executor.recipe_library import resolve_recipe

    recipe: RecipeDefinition | None = resolve_recipe(
        recipe_name, session.definition, recipe_library,
    )
    if recipe is None:
        return {
            "success": False,
            "error": "RecipeNotFound",
            "message": f"recipe '{recipe_name}' は定義されていません",
            "available_recipes": list(session.definition.recipes.keys()),
        }

    # パラメータ検証 (簡易: 必須チェックのみ)
    for p in recipe.parameters:
        if p.required and p.name not in parameters and p.default is None:
            return {
                "success": False,
                "error": "MissingParameter",
                "message": f"必須パラメータ '{p.name}' が指定されていません",
            }
    # default 適用
    variables = dict(parameters)
    for p in recipe.parameters:
        if p.name not in variables and p.default is not None:
            variables[p.name] = p.default

    # Recipe → IR Plan 変換 (definition を渡し ${...} の範囲宣言検証を有効化)
    try:
        plan = recipe_to_plan(
            recipe, variables,
            primary_resource=session.resource_name,
            definition=session.definition,
            session_resolver=session_resolver,
        )
    except (ExpressionError, SeqExpressionError) as e:
        return {
            "success": False,
            "recipe": recipe_name,
            "error": "ExpressionError",
            "message": str(e),
            "steps_executed": [],
        }

    # Plan 実行
    return await execute_plan(
        visa, session, plan,
        recipe_name=recipe_name,
        override_safety=override_safety,
        override_reason=override_reason,
        session_resolver=session_resolver,
    )
