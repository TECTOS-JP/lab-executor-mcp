"""レシピ編集ストア (Web UI M3)。

``--edit-dir`` で明示されたディレクトリ内の機器定義 YAML ファイルだけを
編集対象とする。書き込みは **edit-dir 配下の YAML と git 履歴のみ**。
state DB には一切触れない。

絶対制約:
- パストラバーサル防御: ``_resolve`` が edit_dir 外へのエスケープを拒否する
  (``resolve()`` 後に edit_dir.resolve() 配下であることを検証。シンボリック
  リンク経由の脱出も resolve() が実パスに正規化するため防げる)。
- 検証ゲート: 保存時に必ず ``validate_instrument_file`` で再検証し、errors が
  あれば保存しない (警告のみなら保存可)。
- 検証・パースロジックは再実装せず ``lab_executor.registry`` を import する。
- LF 改行で書き込む (CRLF が来ても LF に正規化)。
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Any

import yaml

from lab_executor.registry import validate_instrument_file

# git author/committer の固定 identity (edit-dir の既存 git 設定に依存しない)。
_GIT_NAME = "lab-executor-ui"
_GIT_EMAIL = "lab-executor-ui@localhost"


class EditStoreError(Exception):
    """編集操作の案内用例外 (パス不正・検証失敗・git 失敗など)。

    app.py の exception handler が JSON 422/400 に変換する。
    """


def _normalize_lf(content: str) -> str:
    """CRLF / CR を LF に正規化する。"""
    return content.replace("\r\n", "\n").replace("\r", "\n")


def _recipe_names(path: Path) -> list[str]:
    """YAML をパースして recipes のキー一覧を返す (失敗時は空)。"""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return []
    if not isinstance(raw, dict):
        return []
    recipes = raw.get("recipes")
    if not isinstance(recipes, dict):
        return []
    return sorted(str(k) for k in recipes.keys())


class EditDirStore:
    """edit-dir 内の機器定義 YAML を列挙・読み書き・git commit する。

    - 新規ファイル作成は M3 では非対応 (既存ファイルの編集のみ)。
    - すべての rel は ``_resolve`` を通してから I/O する。
    """

    def __init__(self, edit_dir: Path | str) -> None:
        p = Path(edit_dir)
        if not p.exists() or not p.is_dir():
            raise EditStoreError(f"edit-dir が存在しません: {p}")
        # 以後の配下判定は resolve() 済みの実パスで行う。
        self._root = p.resolve()

    @property
    def root(self) -> Path:
        return self._root

    # ---------------------------------------------------------------
    # パストラバーサル防御
    # ---------------------------------------------------------------
    def _resolve(self, rel: str) -> Path:
        """rel を edit_dir 配下の実パスに解決する。

        edit_dir 外に出る rel (``..`` / 絶対パス / シンボリックリンク経由) は
        EditStoreError で拒否する。
        """
        if rel is None or str(rel).strip() == "":
            raise EditStoreError("ファイルパスが空です")
        rel_str = str(rel).replace("\\", "/")
        candidate = Path(rel_str)
        if candidate.is_absolute():
            raise EditStoreError(f"絶対パスは許可されません: {rel}")
        # root からの結合後に resolve() し、実パスが root 配下かを検証する。
        resolved = (self._root / candidate).resolve()
        try:
            resolved.relative_to(self._root)
        except ValueError:
            raise EditStoreError(f"edit-dir の外を指すパスは許可されません: {rel}")
        return resolved

    # ---------------------------------------------------------------
    # 列挙 / 読み取り
    # ---------------------------------------------------------------
    def list_files(self) -> list[dict[str, Any]]:
        """*.yaml / *.yml を再帰列挙する (rel path + recipe 名一覧)。"""
        out: list[dict[str, Any]] = []
        for pattern in ("*.yaml", "*.yml"):
            for path in self._root.rglob(pattern):
                if not path.is_file():
                    continue
                rel = path.relative_to(self._root).as_posix()
                out.append({
                    "rel": rel,
                    "recipes": _recipe_names(path),
                })
        out.sort(key=lambda d: d["rel"])
        return out

    def read_file(self, rel: str) -> str:
        """rel の内容を返す。存在しなければ EditStoreError。"""
        path = self._resolve(rel)
        if not path.exists() or not path.is_file():
            raise EditStoreError(f"ファイルが見つかりません: {rel}")
        return path.read_text(encoding="utf-8")

    # ---------------------------------------------------------------
    # 保存 (検証ゲート + git commit)
    # ---------------------------------------------------------------
    def save_file(
        self, rel: str, content: str, message: str = ""
    ) -> dict[str, Any]:
        """検証 → LF 書き込み → git commit の順で保存する。

        1) ``_resolve`` で edit_dir 外へのエスケープを拒否
        2) 一時ファイル経由で validate_instrument_file、errors があれば
           EditStoreError (ファイルは未変更のまま)
        3) LF 改行で書き込み
        4) git add + git commit (repo でなければ git init)。commit 失敗時は
           ``committed=False`` で返し、ファイルは保存済みである旨を区別する。

        返り値: ``{"saved": True, "committed": bool, "commit": <hash|None>,
                   "validation": {...}, "commit_error": <str|None>}``
        """
        path = self._resolve(rel)
        # 既存ファイルの編集のみ (新規作成は M3 スコープ外)。
        if not path.exists() or not path.is_file():
            raise EditStoreError(f"ファイルが見つかりません: {rel}")

        normalized = _normalize_lf(content)

        # ---- 検証ゲート (一時ファイル経由。既存ファイルは触らない) ----
        report = self._validate_content(normalized)
        if report.get("errors"):
            raise EditStoreError(
                "検証エラーのため保存できません: "
                + "; ".join(
                    e.get("message", str(e)) for e in report["errors"]
                )
            )

        # ---- LF で書き込み (newline="" で改行変換を抑止し LF をそのまま出す) ----
        path.write_text(normalized, encoding="utf-8", newline="")

        # ---- git add + commit ----
        committed, commit_hash, commit_error = self._git_commit(path, rel, message)

        return {
            "saved": True,
            "committed": committed,
            "commit": commit_hash,
            "commit_error": commit_error,
            "validation": report,
        }

    def validate(self, content: str) -> dict[str, Any]:
        """content を検証して ValidationReport の dict を返す (保存しない)。"""
        return self._validate_content(_normalize_lf(content))

    def _validate_content(self, content: str) -> dict[str, Any]:
        """content を一時ファイルに書いて validate_instrument_file を実行する。"""
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8", newline=""
        )
        try:
            tmp.write(content)
            tmp.close()
            report = validate_instrument_file(tmp.name)
            return report.to_dict()
        finally:
            try:
                Path(tmp.name).unlink()
            except OSError:
                pass

    # ---------------------------------------------------------------
    # git
    # ---------------------------------------------------------------
    def _git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args],
            cwd=str(self._root),
            capture_output=True,
            text=True,
        )

    def _ensure_repo(self) -> None:
        """edit_dir が git repo でなければ git init する。"""
        r = self._git("rev-parse", "--is-inside-work-tree")
        if r.returncode != 0:
            init = self._git("init")
            if init.returncode != 0:
                raise EditStoreError(
                    f"git init に失敗しました: {init.stderr.strip()}"
                )

    def _git_commit(
        self, path: Path, rel: str, message: str
    ) -> tuple[bool, str | None, str | None]:
        """path を git add + commit する。

        返り値: (committed, commit_hash, error)。commit 失敗時は
        (False, None, <理由>) を返す。**ファイルは既に保存済み** なので
        呼び出し側はこの区別で「保存成功・commit のみ失敗」を案内できる。
        """
        try:
            self._ensure_repo()
        except EditStoreError as exc:
            return False, None, str(exc)

        add = self._git("add", "--", rel)
        if add.returncode != 0:
            return False, None, f"git add 失敗: {add.stderr.strip()}"

        commit_msg = f"ui: edit {rel}"
        if message and message.strip():
            commit_msg += "\n\n" + message.strip()

        commit = self._git(
            "-c", f"user.name={_GIT_NAME}",
            "-c", f"user.email={_GIT_EMAIL}",
            "commit", "-m", commit_msg,
        )
        if commit.returncode != 0:
            # 変更なし (内容同一) も commit 失敗になる。区別して案内する。
            detail = (commit.stdout + commit.stderr).strip()
            if "nothing to commit" in detail or "no changes added" in detail:
                return False, None, "変更がないため commit されませんでした"
            return False, None, f"git commit 失敗: {detail}"

        rev = self._git("rev-parse", "HEAD")
        commit_hash = rev.stdout.strip() if rev.returncode == 0 else None
        return True, commit_hash, None
