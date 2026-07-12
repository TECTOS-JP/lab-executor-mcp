"""実験資産レジストリ (asset publish / catalog, P3.0 / v2.27.0)

ディレクトリ 1 つ + INDEX.yaml + 資産 zip 群で構成される、利用者が任意の場所に
作る資産レジストリ。既存の extension registry (``registry/INDEX.yaml``、instruments
用) とは**別物**であり、そちらには一切触れない。

掲載ゲート:
- publish は check pass (schema_ok かつ checksums_ok) を必須とする。
- ``visibility == "external"`` のレジストリでは、共有ポリシー (2026-07-07 所有者
  決定) に基づき **level_verified <= 3** かつ **license が付与されている**
  (UNLICENSED / 空でない) ことを要求する。
- ``--force`` は同一 asset_id の重複置換にのみ許され、ゲートのスキップには使えない。

正本: docs/asset_registry_p30_plan.md / docs/asset_usage.md「共有ポリシー」節。
"""
from __future__ import annotations

import hashlib
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from lab_executor.asset.checker import check_asset


REGISTRY_VERSION = "0.1"
INDEX_NAME = "INDEX.yaml"
ASSETS_SUBDIR = "assets"
EXTERNAL_LEVEL_CAP = 3


class AssetRegistryError(Exception):
    """資産レジストリ操作のエラー (init/publish/catalog の拒否理由を運ぶ)。"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _index_path(registry_dir: Path) -> Path:
    return registry_dir / INDEX_NAME


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _sort_key(entry: dict[str, Any]) -> tuple:
    """規約順のソートキー: level_verified 降順 → published_at 降順。

    人気指標は持たない (集中リスク対策の設計原則)。
    """
    lv = entry.get("level_verified")
    lv = lv if isinstance(lv, int) else -1
    pub = entry.get("published_at") or ""
    # 降順にしたいので負値 / 反転文字列を使わず、sorted(reverse) 相当を
    # 呼び出し側で行う代わりにここではタプルを返し、呼び出し側で reverse=True。
    return (lv, pub)


def _sorted_assets(assets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(assets, key=_sort_key, reverse=True)


def init_registry(
    registry_dir: Path | str,
    *,
    name: str = "",
    visibility: str = "team",
) -> dict[str, Any]:
    """レジストリを初期化し INDEX.yaml を作成する。

    既存の INDEX があれば AssetRegistryError。visibility は team | external。
    """
    if visibility not in ("team", "external"):
        raise AssetRegistryError(
            f"visibility は team か external: {visibility!r}")
    d = Path(registry_dir)
    idx = _index_path(d)
    if idx.exists():
        raise AssetRegistryError(
            f"registry already initialized (INDEX exists): {idx}")
    d.mkdir(parents=True, exist_ok=True)
    (d / ASSETS_SUBDIR).mkdir(parents=True, exist_ok=True)
    index = {
        "registry_version": REGISTRY_VERSION,
        "visibility": visibility,
        "name": name,
        "created_at": _now_iso(),
        "assets": [],
    }
    _save_index(d, index)
    return index


def _save_index(registry_dir: Path, index: dict[str, Any]) -> None:
    index = dict(index)
    index["assets"] = _sorted_assets(index.get("assets") or [])
    _index_path(registry_dir).write_text(
        yaml.safe_dump(index, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def load_index(registry_dir: Path | str) -> dict[str, Any]:
    """INDEX.yaml を読み込む。無い / 壊れは AssetRegistryError。"""
    d = Path(registry_dir)
    idx = _index_path(d)
    if not idx.exists():
        raise AssetRegistryError(
            f"registry not initialized (no INDEX): {idx}. "
            "先に registry-init を実行してください")
    try:
        data = yaml.safe_load(idx.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        raise AssetRegistryError(f"INDEX が壊れています: {e}") from e
    if not isinstance(data, dict) or "assets" not in data:
        raise AssetRegistryError(f"INDEX の形式が不正です: {idx}")
    if not isinstance(data.get("assets"), list):
        raise AssetRegistryError(f"INDEX.assets が list ではありません: {idx}")
    return data


def _read_asset_manifest(zip_path: Path) -> dict[str, Any]:
    with zipfile.ZipFile(zip_path, "r") as zf:
        return yaml.safe_load(zf.read("asset.yaml").decode("utf-8")) or {}


def _requires_commands_from_zip(zip_path: Path) -> list[str]:
    """資産内 recipe の requires.commands を抽出する (発見性用。無ければ [])。"""
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            if "recipe/recipe.yaml" not in zf.namelist():
                return []
            doc = yaml.safe_load(
                zf.read("recipe/recipe.yaml").decode("utf-8")) or {}
    except Exception:
        return []
    frag = (doc.get("fragment") or {})
    defn = (frag.get("definition") or {})
    requires = defn.get("requires") or {}
    cmds = requires.get("commands") or []
    return [str(c) for c in cmds] if isinstance(cmds, list) else []


def publish_asset(
    zip_path: Path | str,
    registry_dir: Path | str,
    *,
    tags: list[str] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """資産 zip を掲載ゲートを通してレジストリに publish する。

    掲載ゲート (迂回不可):
    1. check pass: schema_ok かつ checksums_ok が必須。
    2. external レジストリ: level_verified <= 3 かつ license が付与されていること。
    3. force は同一 asset_id の**重複置換のみ**に許す (ゲートスキップ不可)。

    Returns:
        {"published": True, "id", "level_verified", "registry", "path"}
    """
    zp = Path(zip_path)
    d = Path(registry_dir)
    if not zp.exists():
        raise AssetRegistryError(f"asset zip not found: {zp}")

    index = load_index(d)  # 無ければ AssetRegistryError で案内

    # --- ゲート 1: check pass ---
    report = check_asset(zp)
    if not report.schema_ok:
        raise AssetRegistryError(
            "publish 拒否: asset.yaml のスキーマ検証に失敗 (schema_ok=False)")
    if not report.checksums_ok:
        raise AssetRegistryError(
            "publish 拒否: 同梱物の checksum が一致しません "
            "(改ざん / 破損の可能性)")

    level_verified = report.level_verified
    manifest = _read_asset_manifest(zp)
    asset_id = manifest.get("asset_id") or report.asset_id
    if not asset_id:
        raise AssetRegistryError("publish 拒否: asset_id を特定できません")
    license_id = (manifest.get("license") or "").strip()
    title = manifest.get("title") or ""
    producer = ((manifest.get("provenance") or {}).get("producer") or "")

    visibility = index.get("visibility", "team")

    # --- ゲート 2: external 共有ゲート (所有者決定の機械化) ---
    if visibility == "external":
        if level_verified > EXTERNAL_LEVEL_CAP:
            raise AssetRegistryError(
                "publish 拒否 (external 共有ゲート): "
                f"level_verified=L{level_verified} は L3 上限を超えます。"
                "共有ポリシー (2026-07-07 所有者決定) により、外部共有は L3 "
                "(再解析可能) まで。L4/L5 (再実行可能物) はチーム内に留めます")
        if not license_id or license_id.upper() == "UNLICENSED":
            raise AssetRegistryError(
                "publish 拒否 (external 共有ゲート): license が付与されていません "
                f"(license={license_id or 'UNLICENSED'!r})。外部共有する資産には "
                "--license で明示的なライセンス (例: CC-BY-4.0) を付与してください")

    # --- ゲート 3: 重複 asset_id ---
    assets: list[dict[str, Any]] = list(index.get("assets") or [])
    existing_idx = next(
        (i for i, e in enumerate(assets) if e.get("id") == asset_id), None)
    if existing_idx is not None and not force:
        raise AssetRegistryError(
            f"publish 拒否: asset_id {asset_id} は既に掲載済みです。"
            "置換するには --force を指定してください "
            "(--force は重複置換のみに使えます。掲載ゲートは迂回できません)")

    # --- zip を assets/ にコピー + sha256 計算 ---
    assets_dir = d / ASSETS_SUBDIR
    assets_dir.mkdir(parents=True, exist_ok=True)
    rel_path = f"{ASSETS_SUBDIR}/{asset_id}.asset.zip"
    dest = d / rel_path
    shutil.copyfile(zp, dest)
    sha256 = _sha256_file(dest)

    entry = {
        "id": asset_id,
        "title": title,
        "level_verified": level_verified,
        "license": license_id or "UNLICENSED",
        "sha256": sha256,
        "path": rel_path,
        "tags": list(tags or []),
        "requires_commands": _requires_commands_from_zip(zp),
        "published_at": _now_iso(),
        "producer": producer,
    }

    # v2.32.0 (SP-6): contains_code 資産の注意表示 (spec §6.1 の共有ゲート表示)。
    # **拒否はしない** (L3 まで許容の既存ゲートは不変 — L3 = 再解析用途では
    # py コードはむしろ解析手順の完全な記録として価値になる)。
    notices: list[str] = []
    contains_code = manifest.get("contains_code")
    if isinstance(contains_code, dict) and (
        contains_code.get("python") or contains_code.get("dll")
    ):
        entry["contains_code"] = {
            "python": bool(contains_code.get("python")),
            "dll": bool(contains_code.get("dll")),
        }
        if visibility == "external":
            notices.append(
                "この資産はコード (py/dll ステップ) を含みます。受け手は"
                "コードを検分の上、自己のポリシー (code_execution) で実行"
                "可否を判断してください (既定では外部資産のコードは実行"
                "されません)"
            )

    if existing_idx is not None:
        assets[existing_idx] = entry
    else:
        assets.append(entry)
    index["assets"] = assets
    _save_index(d, index)

    out: dict[str, Any] = {
        "published": True,
        "id": asset_id,
        "level_verified": level_verified,
        "registry": str(d),
        "path": str(dest),
    }
    if entry.get("contains_code"):
        out["contains_code"] = entry["contains_code"]
    if notices:
        out["notices"] = notices
    return out


def catalog(
    registry_dir: Path | str,
    *,
    recheck: bool = False,
) -> list[dict[str, Any]]:
    """INDEX の assets を規約順 (level 降順 → published_at 降順) で返す。

    recheck=True なら各 zip の sha256 を再計算して INDEX と照合し、不一致 entry に
    ``"integrity": "FAILED"`` を、一致 entry に ``"integrity": "OK"`` を付ける。
    """
    d = Path(registry_dir)
    index = load_index(d)
    assets = _sorted_assets(list(index.get("assets") or []))
    if not recheck:
        return assets

    out: list[dict[str, Any]] = []
    for entry in assets:
        e = dict(entry)
        rel = e.get("path") or ""
        zp = d / rel
        if not zp.exists():
            e["integrity"] = "FAILED"
        else:
            actual = _sha256_file(zp)
            e["integrity"] = "OK" if actual == e.get("sha256") else "FAILED"
        out.append(e)
    return out
