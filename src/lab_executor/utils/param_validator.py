from __future__ import annotations
from lab_executor.models.instrument_def import CommandDefinition, ParameterDefinition


class ParameterValidationError(ValueError):
    pass


def validate_and_build_scpi(
    cmd_def: CommandDefinition,
    params: dict,
) -> str:
    """
    params を CommandDefinition に従って検証し、SCPIコマンド文字列を組み立てる。
    検証エラー時は ParameterValidationError を送出する。
    """
    validated: dict[str, object] = {}

    for param_def in cmd_def.parameters:
        name = param_def.name

        if name not in params:
            if param_def.required:
                raise ParameterValidationError(
                    f"必須パラメータ '{name}' が指定されていません。"
                )
            if param_def.default is not None:
                validated[name] = param_def.default
            continue

        value = params[name]
        validated[name] = _cast_and_check(param_def, value)

    # 余分なパラメータは無視（寛容な設計）
    return cmd_def.scpi.format(**validated)


def _cast_and_check(param_def: ParameterDefinition, value: object) -> object:
    name = param_def.name

    if param_def.type == "integer":
        try:
            v = int(value)
        except (TypeError, ValueError):
            raise ParameterValidationError(
                f"パラメータ '{name}' は整数である必要があります（値: {value!r}）。"
            )
        if param_def.range:
            lo, hi = param_def.range
            if not (lo <= v <= hi):
                raise ParameterValidationError(
                    f"パラメータ '{name}' は {lo}〜{hi} の範囲である必要があります（値: {v}）。"
                )
        return v

    elif param_def.type == "float":
        try:
            v = float(value)
        except (TypeError, ValueError):
            raise ParameterValidationError(
                f"パラメータ '{name}' は数値である必要があります（値: {value!r}）。"
            )
        if param_def.range:
            lo, hi = param_def.range
            if not (lo <= v <= hi):
                raise ParameterValidationError(
                    f"パラメータ '{name}' は {lo}〜{hi} の範囲である必要があります（値: {v}）。"
                )
        return v

    elif param_def.type == "enum":
        v = str(value)
        if param_def.choices and v not in param_def.choices:
            raise ParameterValidationError(
                f"パラメータ '{name}' は {param_def.choices} のいずれかである必要があります（値: {v!r}）。"
            )
        return v

    else:  # string
        return str(value)
