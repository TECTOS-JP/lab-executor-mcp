"""
コード実行ポリシーゲート (v2.32.0 SP-6, sequence_processing_spec §6.1)

py / dll ステップの実行可否をラボポリシーで制御する。

**正直な前提 (信頼モデル)**: py / dll ステップは任意コード実行であり、
信頼境界は「レシピの作者」である。subprocess 分離は安定性対策
(クラッシュ・ハング・メモリ暴走からランタイムを守る) であって
**セキュリティサンドボックスではない**。したがって制御は
「実行の可否 (本モジュール)」と「来歴の完全記録 (timeline)」で行う。

ポリシーファイル: instruments dir (機器定義ディレクトリ) の ``_policy.yaml``。
探索順: 明示引数 → 環境変数 ``LAB_EXECUTOR_POLICY_DIR`` → 既定ポリシー。

```yaml
code_execution:
  python: "allow"          # allow | scripts_dir_only | hash_pinned | deny
  scripts_dir: "./scripts" # file: の許可ディレクトリ (policy ファイル基準の相対可)
  dll: "dir_allowlist"     # allow | dir_allowlist | hash_pinned | deny
  dll_dirs: ["C:/Vendor/AnalysisLib"]
  pinned_hashes: ["sha256:..."]
```

既定値 (spec §6.1): 自分のラボ = ``python: allow`` / ``dll: dir_allowlist``
(**dll_dirs 空 = 事実上 deny**)。外部から受け取った資産の実行時は両方 deny に
するのが推奨運用 (明示的にポリシーを緩めない限り他人のコードは走らない)。
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

POLICY_FILE_NAME = "_policy.yaml"

_PY_MODES = ("allow", "scripts_dir_only", "hash_pinned", "deny")
_DLL_MODES = ("allow", "dir_allowlist", "hash_pinned", "deny")


class CodePolicyError(ValueError):
    """ポリシー違反 / ポリシーファイル不正。"""


@dataclass
class CodePolicy:
    python: str = "allow"
    scripts_dir: Path | None = None
    dll: str = "dir_allowlist"
    dll_dirs: list[Path] = field(default_factory=list)
    pinned_hashes: set[str] = field(default_factory=set)
    source: str = "default"       # policy ファイル path または "default"


def _norm_hash(h: str) -> str:
    h = str(h).strip().lower()
    if h.startswith("sha256:"):
        h = h[len("sha256:"):]
    return h


def sha256_file(path: Path | str) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_policy(policy_dir: Path | str | None = None) -> CodePolicy:
    """ポリシーを読み込む。

    探索順: ``policy_dir`` 引数 → env ``LAB_EXECUTOR_POLICY_DIR`` → 既定。
    ``<dir>/_policy.yaml`` が存在しなければ既定ポリシーを返す。
    ファイルが壊れている場合は ``CodePolicyError`` (安全側: 黙って既定に
    落とさない — 「ポリシーを書いたつもりが効いていない」を防ぐ)。
    """
    base: Path | None = None
    if policy_dir is not None:
        base = Path(policy_dir)
    else:
        env = os.environ.get("LAB_EXECUTOR_POLICY_DIR", "").strip()
        if env:
            base = Path(env)

    if base is None:
        return CodePolicy()
    pf = base / POLICY_FILE_NAME
    if not pf.exists():
        # baseの由来は保持する。compile後に管理者が_policy.yamlを追加した
        # 場合も、IRが同じdirを実行直前に再検査できるようにする。
        return CodePolicy(source=str(pf))

    try:
        raw = yaml.safe_load(pf.read_text(encoding="utf-8")) or {}
    except Exception as e:  # noqa: BLE001
        raise CodePolicyError(f"ポリシーファイルのパースに失敗: {pf} ({e})")
    if not isinstance(raw, dict):
        raise CodePolicyError(f"ポリシーファイルは mapping が必要: {pf}")
    ce = raw.get("code_execution") or {}
    if not isinstance(ce, dict):
        raise CodePolicyError(f"code_execution は mapping が必要: {pf}")

    py_mode = str(ce.get("python", "allow"))
    dll_mode = str(ce.get("dll", "dir_allowlist"))
    if py_mode not in _PY_MODES:
        raise CodePolicyError(
            f"code_execution.python が不正: {py_mode!r} (許可: {_PY_MODES})"
        )
    if dll_mode not in _DLL_MODES:
        raise CodePolicyError(
            f"code_execution.dll が不正: {dll_mode!r} (許可: {_DLL_MODES})"
        )

    scripts_dir_raw = ce.get("scripts_dir", "./scripts")
    scripts_dir = (base / scripts_dir_raw).resolve() \
        if not Path(scripts_dir_raw).is_absolute() \
        else Path(scripts_dir_raw).resolve()

    dll_dirs = []
    for d in ce.get("dll_dirs") or []:
        p = Path(d)
        dll_dirs.append((base / p).resolve() if not p.is_absolute() else p.resolve())

    pinned = {_norm_hash(h) for h in (ce.get("pinned_hashes") or [])}

    return CodePolicy(
        python=py_mode,
        scripts_dir=scripts_dir,
        dll=dll_mode,
        dll_dirs=dll_dirs,
        pinned_hashes=pinned,
        source=str(pf),
    )


def _is_under(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def resolve_py_file(policy: CodePolicy, file_ref: str) -> Path:
    """py の ``file:`` 参照を絶対 path に解決する (scripts_dir 基準)。

    絶対 path はそのまま。相対 path は policy.scripts_dir (無指定時は
    cwd の ./scripts) 基準で解決する。存在しなければ ``CodePolicyError``。
    """
    p = Path(file_ref)
    if not p.is_absolute():
        base = policy.scripts_dir or (Path.cwd() / "scripts")
        p = base / p
    p = p.resolve()
    if not p.exists():
        raise CodePolicyError(f"py.file が存在しません: {p}")
    return p


def check_python(
    policy: CodePolicy,
    *,
    file_path: Path | None,
    sha256: str,
) -> None:
    """python 実行のポリシー適合を検査する。違反は ``CodePolicyError``。

    - deny: 常に拒否
    - scripts_dir_only: ``file:`` かつ scripts_dir 配下のみ (code: は拒否)
    - hash_pinned: sha256 (file はファイル hash / code は全文 hash) が
      pinned_hashes に含まれること
    - allow: 制限なし
    """
    mode = policy.python
    if mode == "deny":
        raise CodePolicyError(
            f"ポリシーにより python コード実行は拒否されています "
            f"(code_execution.python=deny, policy={policy.source})"
        )
    if mode == "scripts_dir_only":
        if file_path is None:
            raise CodePolicyError(
                "ポリシー scripts_dir_only では code: (インライン) は実行"
                f"できません。scripts_dir 配下の file: を使用してください "
                f"(policy={policy.source})"
            )
        sd = policy.scripts_dir
        if sd is None or not _is_under(file_path, sd):
            raise CodePolicyError(
                f"py.file が scripts_dir ({sd}) 配下ではありません: "
                f"{file_path} (policy={policy.source})"
            )
    if mode == "hash_pinned":
        if _norm_hash(sha256) not in policy.pinned_hashes:
            raise CodePolicyError(
                f"py の sha256 が pinned_hashes にありません: sha256:{sha256} "
                f"(policy={policy.source})"
            )


def check_dll(policy: CodePolicy, *, path: Path, sha256: str) -> None:
    """dll 呼び出しのポリシー適合を検査する。違反は ``CodePolicyError``。

    - deny: 常に拒否
    - dir_allowlist: dll_dirs のいずれか配下のみ (**dll_dirs 空 = 事実上 deny**)
    - hash_pinned: DLL ファイルの sha256 が pinned_hashes に含まれること
    - allow: 制限なし
    """
    mode = policy.dll
    if mode == "deny":
        raise CodePolicyError(
            f"ポリシーにより DLL 呼び出しは拒否されています "
            f"(code_execution.dll=deny, policy={policy.source})"
        )
    if mode == "dir_allowlist":
        if not policy.dll_dirs:
            raise CodePolicyError(
                "code_execution.dll=dir_allowlist ですが dll_dirs が空です "
                f"(事実上 deny)。ポリシーで許可ディレクトリを宣言してください "
                f"(policy={policy.source})"
            )
        if not any(_is_under(path, d) for d in policy.dll_dirs):
            raise CodePolicyError(
                f"DLL が許可ディレクトリ配下ではありません: {path} "
                f"(dll_dirs={[str(d) for d in policy.dll_dirs]}, "
                f"policy={policy.source})"
            )
    if mode == "hash_pinned":
        if _norm_hash(sha256) not in policy.pinned_hashes:
            raise CodePolicyError(
                f"DLL の sha256 が pinned_hashes にありません: "
                f"sha256:{sha256} (policy={policy.source})"
            )
