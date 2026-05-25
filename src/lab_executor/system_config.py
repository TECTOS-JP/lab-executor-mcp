"""
System configuration (v0.6.0)

instruments YAML 群とは独立して、システム全体のトポロジを定義する設定。

- instruments alias: 短縮名 (psu001) ↔ VISA resource_name の対応 + bus 帰属
- buses: バス単位の同時アクセス制限
- instrument_groups: 同種機器の集合 (query_group の対象)
- experiment_units: 1 つの実験対象に紐づく機器セット (map_recipe の対象)

ファイル配置: `instruments/_system.yaml` (instrument 定義と同じディレクトリ、underscore prefix で
個別機器 YAML と区別)。存在しなくてもサーバは起動する (v0.5 系互換)。
"""
from __future__ import annotations
import logging
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, model_validator

logger = logging.getLogger(__name__)


# GPIB resource を検出する正規表現 (default bus 推定用)
_GPIB_RE = re.compile(r"^GPIB(\d+)::", re.IGNORECASE)


class InstrumentBinding(BaseModel):
    """logical alias → resource_name のバインディング + 物理 bus 帰属"""
    resource: str                       # "GPIB0::1::INSTR" など
    bus: str = ""                       # "gpib0" / "usb_hub_1" / "lan_segment_1" 等
    description: str = ""

    @model_validator(mode="after")
    def _infer_bus_from_gpib(self) -> "InstrumentBinding":
        """bus 未指定時、resource が GPIB なら自動推定 (GPIB0::... → 'GPIB0')"""
        if not self.bus:
            m = _GPIB_RE.match(self.resource)
            if m:
                self.bus = f"GPIB{m.group(1)}"
        return self


class BusConfig(BaseModel):
    """バス単位の同時アクセス制限"""
    max_concurrency: int = 1
    description: str = ""


class InstrumentGroup(BaseModel):
    """同種機器の集合 (start_group_query_job の対象)"""
    members: list[str] = Field(default_factory=list)
    description: str = ""


class ExperimentUnit(BaseModel):
    """1 つの実験対象に紐づく機器セット (map_recipe の bindings に展開)

    例:
      unit001:
        psu: psu001
        temp: temp001
        dmm: dmm001
    """
    # 任意の role 名 → alias の辞書。すべてのキーを bindings として扱う
    bindings: dict[str, str] = Field(default_factory=dict)
    description: str = ""

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> "ExperimentUnit":
        """YAML 上の `unit001: { psu: psu001, temp: temp001 }` を解釈する。

        専用フィールド `description` は分離し、それ以外を bindings として扱う。
        """
        description = raw.pop("description", "") if isinstance(raw, dict) else ""
        # raw が直接 description キーを持つ dict 想定。残りは bindings
        bindings = {k: v for k, v in raw.items() if k != "description"}
        return cls(bindings=bindings, description=description)


class SystemConfig(BaseModel):
    """system_config root"""
    instruments: dict[str, InstrumentBinding] = Field(default_factory=dict)
    buses: dict[str, BusConfig] = Field(default_factory=dict)
    instrument_groups: dict[str, InstrumentGroup] = Field(default_factory=dict)
    experiment_units: dict[str, ExperimentUnit] = Field(default_factory=dict)

    # ---------- public helpers ----------

    def resolve_alias(self, alias: str) -> str | None:
        """alias を resource_name へ。alias が VISA resource にも見える場合は素通し。"""
        if alias in self.instruments:
            return self.instruments[alias].resource
        # alias が既に resource 形式なら素通し (後方互換)
        if "::" in alias:
            return alias
        return None

    def bus_of(self, alias_or_resource: str) -> str | None:
        """alias または resource 名から所属 bus を取得 (なければ None)"""
        if alias_or_resource in self.instruments:
            return self.instruments[alias_or_resource].bus or None
        # resource として登録されているか逆引き
        for binding in self.instruments.values():
            if binding.resource == alias_or_resource:
                return binding.bus or None
        # 直接 GPIB resource なら推定
        m = _GPIB_RE.match(alias_or_resource)
        if m:
            return f"GPIB{m.group(1)}"
        return None

    def get_group(self, name: str) -> InstrumentGroup | None:
        return self.instrument_groups.get(name)

    def get_unit(self, name: str) -> ExperimentUnit | None:
        return self.experiment_units.get(name)

    # ---------- loader ----------

    @classmethod
    def from_yaml(cls, path: Path) -> "SystemConfig":
        """YAML から SystemConfig を構築 (ファイル無ければ empty を返す)"""
        if not path.exists():
            return cls()
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

        # experiment_units の特殊解釈 (bindings は flat dict)
        units_raw = raw.get("experiment_units") or {}
        units: dict[str, ExperimentUnit] = {}
        if isinstance(units_raw, dict):
            for name, body in units_raw.items():
                if isinstance(body, dict):
                    # body をシャローコピーして from_raw に渡す
                    units[name] = ExperimentUnit.from_raw(dict(body))

        cfg = cls(
            instruments={
                k: InstrumentBinding(**v) for k, v in (raw.get("instruments") or {}).items()
            },
            buses={
                k: BusConfig(**v) for k, v in (raw.get("buses") or {}).items()
            },
            instrument_groups={
                k: InstrumentGroup(**v)
                for k, v in (raw.get("instrument_groups") or {}).items()
            },
            experiment_units=units,
        )

        # GPIB が members に含まれており bus 設定が無ければ GPIBn default を足す
        # (実装方針 #15: GPIB は max_concurrency=1 がデフォルト)
        for alias, binding in cfg.instruments.items():
            if binding.bus and binding.bus not in cfg.buses:
                if binding.bus.upper().startswith("GPIB"):
                    cfg.buses[binding.bus] = BusConfig(
                        max_concurrency=1,
                        description=f"auto: {binding.bus} (GPIB default)",
                    )
                else:
                    # USB / LAN 系は明示が必要だが、unknown はデフォルト大きめにしておく
                    cfg.buses[binding.bus] = BusConfig(
                        max_concurrency=8,
                        description=f"auto: {binding.bus} (default fallback)",
                    )

        logger.info(
            "SystemConfig loaded: instruments=%d, buses=%d, groups=%d, units=%d",
            len(cfg.instruments), len(cfg.buses),
            len(cfg.instrument_groups), len(cfg.experiment_units),
        )
        return cfg
