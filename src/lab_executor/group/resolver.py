"""
v0.6.0 Resolver ── logical ref → resource_name

map_recipe / start_group_query_job で利用される名前解決ロジック。

resolve_resource("psu001", bindings={}, system_config) → "GPIB0::6::INSTR"
resolve_resource("$psu", bindings={"psu": "psu001"}, system_config) → "GPIB0::6::INSTR"
resolve_resource("GPIB0::1::INSTR", bindings={}, system_config) → "GPIB0::1::INSTR"

bindings は { logical_role: alias_or_resource } の辞書。
"$role" が来たら bindings から alias を引き、その alias を system_config で
resource_name に変換する。
"""
from __future__ import annotations
from typing import Any

from lab_executor.system_config import SystemConfig, ExperimentUnit


class ResolveError(Exception):
    pass


def resolve_resource(
    ref: str,
    bindings: dict[str, str],
    system_config: SystemConfig,
) -> str:
    """
    ref を実 resource_name に解決する。

    優先順位:
      1. "$role" 形式: bindings[role] (alias or resource) を取得 → 必要なら alias を resource 化
      2. alias 形式 (system_config.instruments のキー): resource_name に変換
      3. resource 形式 ("::" を含む): 素通し
      4. 上記すべて失敗: ResolveError
    """
    if not ref:
        raise ResolveError("resource ref が空です")

    if ref.startswith("$"):
        role = ref[1:]
        if role not in bindings:
            raise ResolveError(
                f"binding 未指定: '{ref}' に対応する '{role}' が bindings にありません"
            )
        target = bindings[role]
        # alias なら resource_name に展開
        resolved = system_config.resolve_alias(target)
        if resolved is None:
            raise ResolveError(
                f"binding '{role}' → '{target}' が alias / resource として解決できません"
            )
        return resolved

    # alias の直接指定
    resolved = system_config.resolve_alias(ref)
    if resolved is not None:
        return resolved

    raise ResolveError(
        f"'{ref}' は alias でも resource_name でもありません "
        f"(instruments セクションまたは VISA resource 形式 '...::INSTR' で指定)"
    )


def resolve_unit_bindings(
    unit_name: str | None,
    explicit_bindings: dict[str, str] | None,
    system_config: SystemConfig,
) -> dict[str, str]:
    """
    map_recipe target の bindings を構築する。

    優先順位:
      1. unit_name が指定されていれば system_config から取得して bindings の base にする
      2. explicit_bindings は merge / override (明示指定優先)
    """
    base: dict[str, str] = {}
    if unit_name:
        unit = system_config.get_unit(unit_name)
        if unit is None:
            raise ResolveError(f"experiment_unit '{unit_name}' が定義されていません")
        base.update(unit.bindings)
    if explicit_bindings:
        base.update(explicit_bindings)
    return base


def collect_target_resources(
    bindings: dict[str, str],
    system_config: SystemConfig,
) -> list[str]:
    """
    1 つの target に紐づく resource_name のリスト (canonical sorted)。

    bindings の全 alias を resource_name に展開して set で重複排除。
    deadlock 回避のため必ず canonical sorted で返す。
    """
    resources: set[str] = set()
    for role, alias in bindings.items():
        try:
            r = resolve_resource(alias, {}, system_config)  # alias は $ 形式ではない
            resources.add(r)
        except ResolveError:
            # 後段で明示エラーにする
            raise
    return sorted(resources)
