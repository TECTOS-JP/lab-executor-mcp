"""Web UI M3 (レシピエディタ: 検証 → dry-run → git 保存) のテスト。

fixture: tmp_path に git 無しの edit_dir を作り、recipes 付きの機器定義 YAML
(examples/instruments/kikusui_pmx35_3a.yaml を流用) を配置する。

絶対制約の確認:
- 書き込みは edit_dir 配下の YAML + git のみ (state DB は read-only)。
- パストラバーサル防御を最初に検証する。
- 検証エラーのある内容は保存できない (サーバ側で保存時に必ず再検証)。
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lab_executor.ui.app import create_app

_EXAMPLE = (
    Path(__file__).resolve().parents[1]
    / "examples" / "instruments" / "kikusui_pmx35_3a.yaml"
)
REL = "kikusui_pmx35_3a.yaml"


@pytest.fixture
def edit_dir(tmp_path):
    d = tmp_path / "defs"
    d.mkdir()
    shutil.copy(_EXAMPLE, d / REL)
    # ネストしたファイルも 1 つ置いて列挙の再帰を確認する。
    sub = d / "nested"
    sub.mkdir()
    shutil.copy(_EXAMPLE, sub / "copy.yaml")
    return d


@pytest.fixture
def db_path(tmp_path):
    # M3 でも state DB は read-only。ここでは空 (不在) でよいが、
    # モニタルートの疎通確認のため空 JobStore をシードしておく。
    from lab_executor.job.store import JobStore
    p = tmp_path / "state.sqlite"
    store = JobStore(db_path=p)
    store.close()
    return p


@pytest.fixture
def edit_client(db_path, edit_dir):
    app = create_app(db_path, edit_dir=edit_dir)
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def noedit_client(db_path):
    app = create_app(db_path)
    return TestClient(app, raise_server_exceptions=False)


# ============================================================
# 編集無効時
# ============================================================


def test_edit_disabled_without_dir(noedit_client):
    assert noedit_client.get("/recipes").status_code == 404
    assert noedit_client.get("/api/edit/files").status_code == 404
    assert noedit_client.get(f"/api/edit/file/{REL}").status_code == 404
    r = noedit_client.post(
        "/api/edit/validate", json={"rel": REL, "content": "x"}
    )
    assert r.status_code == 404


# ============================================================
# 列挙 / 読み取り
# ============================================================


def test_list_and_read(edit_client):
    r = edit_client.get("/api/edit/files")
    assert r.status_code == 200
    files = r.json()["files"]
    rels = {f["rel"] for f in files}
    assert REL in rels
    assert "nested/copy.yaml" in rels
    # レシピ名が列挙されている
    entry = next(f for f in files if f["rel"] == REL)
    assert "safe_output_on" in entry["recipes"]

    # 読み取り内容が一致
    r2 = edit_client.get(f"/api/edit/file/{REL}")
    assert r2.status_code == 200
    assert "safe_output_on" in r2.json()["content"]

    # HTML 一覧・エディタも 200
    assert edit_client.get("/recipes").status_code == 200
    assert edit_client.get(f"/recipes/edit/{REL}").status_code == 200


# ============================================================
# パストラバーサル防御
# ============================================================


@pytest.mark.parametrize(
    "rel",
    ["../outside.yaml", "../../etc/passwd", "nested/../../escape.yaml"],
)
def test_path_traversal_blocked_read(edit_client, rel):
    # URL 内の `..` は ASGI/クライアント側で正規化され route に到達しない
    # (404) か、到達しても _resolve が 422 で拒否する。いずれも「外へ出られ
    # ない」= 防御成立。
    r = edit_client.get(f"/api/edit/file/{rel}")
    assert r.status_code in (400, 404, 422)


@pytest.mark.parametrize(
    "rel",
    ["../outside.yaml", "../../etc/passwd", "nested/../../escape.yaml",
     "/abs/path.yaml"],
)
def test_resolve_rejects_escape(edit_dir, rel):
    """EditDirStore._resolve が edit_dir 外を指す rel を必ず拒否する (単体)。"""
    from lab_executor.ui.edit_store import EditDirStore, EditStoreError
    store = EditDirStore(edit_dir)
    with pytest.raises(EditStoreError):
        store._resolve(rel)


def test_path_traversal_blocked_absolute(edit_client, tmp_path):
    # 絶対パス相当を validate に投げても拒否される。
    outside = (tmp_path / "evil.yaml").as_posix()
    r = edit_client.post(
        "/api/edit/validate",
        json={"rel": outside, "content": "metadata: {}"},
    )
    assert r.status_code == 422


def test_path_traversal_blocked_save(edit_client):
    r = edit_client.post(
        "/api/edit/save",
        json={"rel": "../escape.yaml", "content": "x", "message": ""},
    )
    assert r.status_code == 422


# ============================================================
# 検証
# ============================================================


def test_validate_ok_and_error(edit_client):
    good = edit_client.get(f"/api/edit/file/{REL}").json()["content"]
    r = edit_client.post(
        "/api/edit/validate", json={"rel": REL, "content": good}
    )
    assert r.status_code == 200
    v = r.json()["validation"]
    assert v["status"] in ("ok", "warning")
    assert v["errors"] == []

    # 壊した YAML (metadata 必須欠落 → schema_invalid)
    r2 = edit_client.post(
        "/api/edit/validate",
        json={"rel": REL, "content": "commands: {}\n"},
    )
    assert r2.status_code == 200
    v2 = r2.json()["validation"]
    assert v2["status"] == "error"
    assert len(v2["errors"]) >= 1


# ============================================================
# dry-run
# ============================================================


def test_dryrun_expands_expressions(edit_client):
    content = edit_client.get(f"/api/edit/file/{REL}").json()["content"]
    r = edit_client.post(
        "/api/edit/dryrun",
        json={
            "rel": REL,
            "content": content,
            "recipe": "safe_output_on",
            "parameters": {"target_v": 10.0, "current_limit": 2.0},
        },
    )
    assert r.status_code == 200
    d = r.json()["dryrun"]
    assert d["step_count"] >= 6
    # set_voltage_protection の voltage = 10 * 1.1 + 0.5 = 11.5 に解決される
    ovp = next(
        s for s in d["steps"] if s["command"] == "set_voltage_protection"
    )
    assert abs(float(ovp["args"]["voltage"]) - 11.5) < 1e-9
    # set_voltage は target_v = 10 に解決
    sv = next(s for s in d["steps"] if s["command"] == "set_voltage")
    assert abs(float(sv["args"]["voltage"]) - 10.0) < 1e-9


def test_dryrun_bad_params(edit_client):
    content = edit_client.get(f"/api/edit/file/{REL}").json()["content"]
    # 必須パラメータ (target_v, current_limit) を欠落させる
    r = edit_client.post(
        "/api/edit/dryrun",
        json={
            "rel": REL,
            "content": content,
            "recipe": "safe_output_on",
            "parameters": {},
        },
    )
    assert r.status_code == 422
    assert "detail" in r.json()


# ============================================================
# 保存 (git commit)
# ============================================================


def _git(edit_dir: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(edit_dir),
        capture_output=True, text=True,
    ).stdout


def test_save_creates_git_commit(edit_client, edit_dir):
    content = edit_client.get(f"/api/edit/file/{REL}").json()["content"]
    modified = content + "\n# edited by test\n"
    r = edit_client.post(
        "/api/edit/save",
        json={"rel": REL, "content": modified, "message": "test edit"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["saved"] is True
    assert body["committed"] is True
    assert body["commit"]

    # git log に commit がある
    log = _git(edit_dir, "log", "--oneline")
    assert "ui: edit" in log

    # ファイル内容が一致し、LF 保存 (CRLF が無い)
    raw = (edit_dir / REL).read_bytes()
    assert b"\r\n" not in raw
    assert raw.decode("utf-8").endswith("# edited by test\n")


def test_save_rejects_invalid(edit_client, edit_dir):
    before = (edit_dir / REL).read_bytes()
    r = edit_client.post(
        "/api/edit/save",
        json={"rel": REL, "content": "commands: {}\n", "message": "bad"},
    )
    assert r.status_code == 422
    # ファイルは未変更のまま
    assert (edit_dir / REL).read_bytes() == before


def test_save_crlf_normalized(edit_client, edit_dir):
    content = edit_client.get(f"/api/edit/file/{REL}").json()["content"]
    crlf = content.replace("\n", "\r\n") + "\r\n# crlf line\r\n"
    r = edit_client.post(
        "/api/edit/save",
        json={"rel": REL, "content": crlf, "message": ""},
    )
    assert r.status_code == 200
    raw = (edit_dir / REL).read_bytes()
    assert b"\r\n" not in raw
    assert raw.decode("utf-8").endswith("# crlf line\n")


def test_save_requires_json_content_type(edit_client):
    content = edit_client.get(f"/api/edit/file/{REL}").json()["content"]
    r = edit_client.post(
        "/api/edit/save",
        content=f"rel={REL}",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    # JSON でない body は 422 (FastAPI のパース or _require_json)
    assert r.status_code == 422


# ============================================================
# モニタルートが編集有効時も従来どおり
# ============================================================


def test_monitor_routes_unaffected(edit_client):
    assert edit_client.get("/").status_code == 200
    r = edit_client.get("/api/jobs")
    assert r.status_code == 200
    assert "jobs" in r.json()


# ============================================================
# SP-7 (v2.34.0): サブシーケンス ライブラリブラウザ
# ============================================================

_SEQ_LIB_YAML = """
sequences:
  stabilize_and_measure:
    description: "N 回測定して平均"
    roles:
      - { name: meter, requires: { commands: [measure_voltage] } }
    parameters:
      - { name: n, type: integer, default: 5 }
    returns: [v_avg, v_std]
    steps:
      - { command: measure_voltage, instrument: "@meter", result_as: v }
"""


@pytest.fixture
def edit_client_with_seq(db_path, tmp_path):
    d = tmp_path / "defs_seq"
    d.mkdir()
    (d / "std_lib.yaml").write_text(_SEQ_LIB_YAML, encoding="utf-8")
    app = create_app(db_path, edit_dir=d)
    return TestClient(app, raise_server_exceptions=False)


def test_api_sequences(edit_client_with_seq):
    r = edit_client_with_seq.get("/api/edit/sequences")
    assert r.status_code == 200
    seqs = r.json()["sequences"]
    assert len(seqs) == 1
    s = seqs[0]
    assert s["call_key"] == "std_lib.stabilize_and_measure"
    assert s["roles"] == ["meter"]
    assert s["returns"] == ["v_avg", "v_std"]
    assert s["parameters"][0]["name"] == "n"
    assert s["parameters"][0]["default"] == 5


def test_recipes_page_shows_library(edit_client_with_seq):
    r = edit_client_with_seq.get("/recipes")
    assert r.status_code == 200
    assert "サブシーケンス ライブラリ" in r.text
    assert "std_lib.stabilize_and_measure" in r.text


def test_sequences_empty_without_seq(edit_client):
    """sequences を持たない edit-dir では空リスト。"""
    r = edit_client.get("/api/edit/sequences")
    assert r.status_code == 200
    assert r.json()["sequences"] == []
