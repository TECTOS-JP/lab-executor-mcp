"""capability 照合 (L4 判定基盤, v2.25.0)

レシピの ``requires:`` (CapabilityRequirements) と、同梱する装置定義
(InstrumentDefinition) を機械照合し、代替装置での追試可能性 (L4) を判定する。

純関数: I/O しない。checker が装置定義を読み込んで渡す。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from lab_executor.models.instrument_def import (
        CapabilityRequirements,
        InstrumentDefinition,
    )


def match_capabilities(
    req: "CapabilityRequirements | None",
    definition: "InstrumentDefinition | None",
) -> dict[str, Any]:
    """要求 capability を装置定義が満たすか照合する。

    Returns:
        {
          "satisfied": bool,
          "missing_commands": [<command>, ...],
          "range_violations": [{"key", "required", "device", "reason"}, ...],
          "range_unknown": [<key>, ...],   # 制約情報が無く比較できなかったもの
        }

    照合規則:
    - ``req.commands`` の各コマンドが ``definition.commands`` に存在すること。
    - ``req.ranges`` (key="<command>.<arg>") は、装置定義の該当コマンド引数に
      ``range: [min, max]`` 制約がある場合のみ比較する。制約が無ければ
      ``range_unknown`` に積み、satisfied は下げない (warning 相当)。
    """
    result: dict[str, Any] = {
        "satisfied": True,
        "missing_commands": [],
        "range_violations": [],
        "range_unknown": [],
    }
    if req is None:
        return result
    if definition is None:
        # 定義が無ければ全要求が満たせない
        result["missing_commands"] = list(req.commands)
        result["satisfied"] = not req.commands and not req.ranges
        return result

    dev_commands = definition.commands or {}

    # 1. commands
    for cmd in req.commands:
        if cmd not in dev_commands:
            result["missing_commands"].append(cmd)

    # 2. ranges ("<command>.<arg>")
    for key, spec in (req.ranges or {}).items():
        cmd_name, _, arg_name = key.partition(".")
        cmd_def = dev_commands.get(cmd_name)
        if cmd_def is None:
            # コマンド自体が無い → missing_commands 側で拾えていなければ追加
            if cmd_name not in result["missing_commands"]:
                result["missing_commands"].append(cmd_name)
            continue
        # 引数の range 制約を探す
        param = None
        for p in cmd_def.parameters:
            if p.name == arg_name:
                param = p
                break
        dev_range = getattr(param, "range", None) if param is not None else None
        if not dev_range or len(dev_range) < 2:
            result["range_unknown"].append(key)
            continue
        dev_min, dev_max = float(dev_range[0]), float(dev_range[1])
        # 要求範囲が装置範囲に収まるか (要求 min/max が装置外なら違反)
        if spec.min is not None and spec.min < dev_min:
            result["range_violations"].append({
                "key": key,
                "required": {"min": spec.min, "max": spec.max},
                "device": {"min": dev_min, "max": dev_max},
                "reason": "required_min_below_device_min",
            })
        if spec.max is not None and spec.max > dev_max:
            result["range_violations"].append({
                "key": key,
                "required": {"min": spec.min, "max": spec.max},
                "device": {"min": dev_min, "max": dev_max},
                "reason": "required_max_above_device_max",
            })

    result["satisfied"] = (
        not result["missing_commands"] and not result["range_violations"]
    )
    return result
