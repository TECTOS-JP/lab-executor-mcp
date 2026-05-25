"""
機器情報・安全制約を LLM に提供するためのツール (v0.2.0)

v0.7.0: describe_instrument / get_state / get_last_measurement を追加。
"""
from __future__ import annotations
import logging
from typing import Any, TYPE_CHECKING

from fastmcp import FastMCP

from lab_executor.response_envelope import make_envelope, make_error
from lab_executor.state_query import query_all_state
from lab_executor.utils.param_validator import validate_and_build_scpi, ParameterValidationError
from lab_executor import safety as sf

if TYPE_CHECKING:
    # v1.11: backend layer top-level import 排除 (型ヒントのみ)
    from visa_mcp.session_manager import SessionManager
    from visa_mcp.visa_manager import VisaManager

logger = logging.getLogger(__name__)


def register_tools(
    mcp: FastMCP,
    session_mgr: SessionManager,
    visa: VisaManager | None = None,
    job_mgr=None,
) -> None:
    """v0.7.0: visa / job_mgr が渡された場合のみ get_state / get_last_measurement /
    describe_instrument を登録。後方互換のため Optional。"""

    @mcp.tool()
    async def get_instrument_info(resource_name: str) -> dict:
        """
        識別済み機器の YAML 定義から、機器仕様・安全制約・推奨手順・応答フォーマットなど
        全情報を一括取得する。LLM が機器の能力と制約を把握するために使用する。

        resource_name: VISA リソース文字列
        """
        session = session_mgr.get_session(resource_name)
        if session is None or session.definition is None:
            return {
                "success": False,
                "error": "SessionNotFound",
                "message": f"{resource_name} は未識別、または YAML 定義がありません。",
            }

        d = session.definition
        return {
            "success": True,
            "data": {
                "metadata": d.metadata.model_dump(),
                "identification": d.identification.model_dump(),
                "connection": d.connection.model_dump(),
                "safety": d.safety.model_dump(),
                "specifications": d.specifications.model_dump(),
                "response_formats": {k: v.model_dump() for k, v in d.response_formats.items()},
                # v0.3.0: 新セクション
                "operational_states": d.operational_states.model_dump(),
                "physical_interface": d.physical_interface.model_dump(),
                "recipes": {k: v.model_dump() for k, v in d.recipes.items()},
                "recipe_count": len(d.recipes),
                "command_count": len(d.commands),
                "command_names": list(d.commands.keys()),
                "safety_mode": sf.get_safety_mode(),
            },
        }

    @mcp.tool()
    async def list_safety_constraints(resource_name: str) -> dict:
        """
        指定機器の安全制約のみを抽出して返す。
        値制約 (ratings)・前提条件 (preconditions)・禁止行為 (cautions)・
        ハードウェア保護 (hardware_protections) を含む。

        resource_name: VISA リソース文字列
        """
        session = session_mgr.get_session(resource_name)
        if session is None or session.definition is None:
            return {
                "success": False,
                "error": "SessionNotFound",
                "message": f"{resource_name} は未識別、または YAML 定義がありません。",
            }
        d = session.definition
        return {
            "success": True,
            "data": {
                "instrument": d.display_name,
                "safety_mode": sf.get_safety_mode(),
                "safety": d.safety.model_dump(),
            },
        }

    @mcp.tool()
    async def validate_operation(
        resource_name: str,
        command_name: str,
        parameters: dict = {},
    ) -> dict:
        """
        実行せずに、コマンドが安全制約に違反するかを事前確認する (dry-run)。
        LLM が「これを送って大丈夫か？」を判断するために使う。

        resource_name: VISA リソース文字列
        command_name: YAML 定義のコマンドキー
        parameters: コマンドパラメータ辞書
        """
        session = session_mgr.get_session(resource_name)
        if session is None or session.definition is None:
            return {
                "success": False,
                "error": "SessionNotFound",
                "message": f"{resource_name} は未識別、または YAML 定義がありません。",
            }

        d = session.definition
        cmd_def = d.commands.get(command_name)
        if cmd_def is None:
            return {
                "success": False,
                "error": "CommandNotFound",
                "message": f"コマンド '{command_name}' は定義されていません。",
                "available_commands": list(d.commands.keys()),
            }

        # パラメータ検証
        param_errors: list[str] = []
        try:
            scpi = validate_and_build_scpi(cmd_def, parameters)
        except ParameterValidationError as e:
            param_errors.append(str(e))
            scpi = None

        # 安全制約検証
        violations = sf.validate(d, command_name, parameters, session_history=session.command_history)
        mode = sf.get_safety_mode()

        valid = not param_errors and not violations
        return {
            "success": True,
            "data": {
                "valid": valid,
                "scpi_to_send": scpi,
                "parameter_errors": param_errors,
                "safety_violations": list(violations),
                "safety_mode": mode,
                "would_block": bool(violations) and mode != "permissive",
                "can_override": bool(violations) and mode == "advisory",
            },
        }

    # =====================================================================
    # v0.7.0: describe_instrument / get_state / get_last_measurement
    # =====================================================================

    if visa is None:
        return  # job_mgr / visa が無ければここで終了

    @mcp.tool()
    async def describe_instrument(resource_name: str) -> dict:
        """機器の能力サマリを LLM 向け構造化 JSON で返す (v0.7.0)

        get_instrument_info より高レベルかつ要約。
        capabilities / state_keys / recommended_usage を含む。
        """
        session = session_mgr.get_session(resource_name)
        if session is None or session.definition is None:
            return make_envelope(
                "error",
                errors=[make_error(
                    "not_found",
                    f"{resource_name} は未識別、または YAML 定義がありません。",
                    recoverable=False,
                )],
            )
        d = session.definition
        data = {
            "resource_name": resource_name,
            "identity": {
                "manufacturer": d.metadata.manufacturer,
                "model": d.metadata.model,
                "description": d.metadata.description,
            },
            "category": d.metadata.category,
            "capabilities": {
                "commands": list(d.commands.keys()),
                "recipes": list(d.recipes.keys()),
                "state_keys": list(d.state_query.keys()),
                "safe_shutdown_defined": bool(d.safe_shutdown),
            },
            "safety": {
                "mode": sf.get_safety_mode(),
                "ratings": {k: v.model_dump() for k, v in d.safety.ratings.items()},
                "cautions_count": len(d.safety.cautions),
                "preconditions_count": len(d.safety.preconditions),
            },
            "polling_safe_commands": [
                k for k, v in d.commands.items() if v.polling_safe
            ],
            "verify_enabled_commands": [
                k for k, v in d.commands.items() if v.verify is not None
            ],
            "recommended_usage": {
                "long_running": "start_recipe_job を使用 (進捗は get_job_status)",
                "polling": (
                    "wait_for_stable / wait_for_condition / start_monitor を選択。"
                    "polling_safe=True の command を推奨"
                ),
                "verify_writes": (
                    "verify を持つ command は write 後に自動で read-back 検証されます"
                    if any(v.verify for v in d.commands.values())
                    else "verify 未定義 (write 後の自動検証は行われません)"
                ),
                "state_inspection": (
                    "get_state で state_keys を取得可能"
                    if d.state_query else "state_query 未定義 (get_state 利用不可)"
                ),
            },
        }
        return make_envelope("ok", data=data)

    @mcp.tool()
    async def get_state(
        resource_name: str,
        keys: list[str] | None = None,
        max_age_s: float = 0.0,
    ) -> dict:
        """機器の現在状態を state_query 定義に従って取得 (v0.7.0)

        resource_name: VISA resource 名
        keys: 取得する state_query キー (空/None なら全件)
        max_age_s: measurement_cache で 0 < age <= max_age_s なら cache を返す。
                   0 (デフォルト) なら必ず実機 query。

        返り値 data.state は {key: {value, unit, age_s, ...}, ...}
        """
        session = session_mgr.get_session(resource_name)
        if session is None or session.definition is None:
            return make_envelope(
                "error",
                errors=[make_error(
                    "not_found",
                    f"{resource_name} は未識別です",
                    recoverable=False,
                )],
            )
        if not session.definition.state_query:
            return make_envelope(
                "error",
                errors=[make_error(
                    "validation",
                    f"{resource_name} の state_query が定義されていません",
                    recoverable=False,
                )],
            )

        target_keys = keys or list(session.definition.state_query.keys())
        # cache lookup
        from datetime import datetime, timezone
        from lab_executor.state_query import query_state_item
        state: dict[str, Any] = {}
        now = datetime.now(timezone.utc)
        store = job_mgr.store if job_mgr is not None else None
        for k in target_keys:
            if k not in session.definition.state_query:
                state[k] = {"value": None, "error": "unknown_key"}
                continue
            item = session.definition.state_query[k]
            cached = None
            if store is not None and max_age_s > 0:
                cached = store.get_measurement_cache(resource_name, k)
            use_cache = False
            if cached is not None:
                try:
                    ts = datetime.fromisoformat(cached["timestamp"])
                    age = (now - ts).total_seconds()
                    if 0 <= age <= max_age_s:
                        state[k] = {
                            "value": cached["value"],
                            "unit": cached["unit"],
                            "age_s": round(age, 3),
                            "cached": True,
                            "timestamp": cached["timestamp"],
                        }
                        use_cache = True
                except Exception:
                    use_cache = False
            if not use_cache:
                r = await query_state_item(visa, session, k, item)
                value = r.get("value")
                state[k] = {
                    "value": value,
                    "unit": item.unit,
                    "age_s": 0.0,
                    "cached": False,
                }
                if r.get("error"):
                    state[k]["error"] = r["error"]
                    state[k]["message"] = r.get("message")
                # cache 更新
                if store is not None and value is not None and not r.get("error"):
                    try:
                        store.upsert_measurement_cache(
                            resource_name, k, value, unit=item.unit,
                        )
                    except Exception as e:
                        logger.warning("measurement_cache 更新失敗: %s", e)

        return make_envelope("ok", data={
            "resource_name": resource_name,
            "state": state,
        })

    @mcp.tool()
    async def get_last_measurement(
        instrument: str,
        measurement: str,
        max_age_s: float = 60.0,
        refresh_if_stale: bool = False,
    ) -> dict:
        """測定値キャッシュから最新値を取得 (v0.7.0 / v0.7.0.1)

        **v0.7.0.1 動作変更 (重要)**:
        - default は `refresh_if_stale=False`: cache が古ければ value=None +
          stale=True を返し、**実機 query を発生させない** (副作用回避の安全側)
        - `refresh_if_stale=True` を明示すると、cache が古い場合に state_query
          定義の command を実行して値を取得する (副作用ある query では注意)

        instrument: VISA resource 名 / alias
        measurement: state_query のキー名 (例: "voltage")
        max_age_s: cache 受容年齢 (s)
        refresh_if_stale: True なら age > max_age_s 時に実機 query で再取得。
                          False (default) なら value=None で返す。

        副作用注意: refresh_if_stale=True かつ state_query の command が
        polling_safe=False の場合、意図せず実機を駆動する。LLM は明示的に
        意図がある場合のみ True を指定すること。
        """
        store = job_mgr.store if job_mgr is not None else None
        if store is None:
            return make_envelope(
                "error",
                errors=[make_error("internal", "store not available")],
            )

        session = session_mgr.get_session(instrument)
        if session is None or session.definition is None:
            return make_envelope(
                "error",
                errors=[make_error("not_found", f"{instrument} は未識別です")],
            )

        from datetime import datetime, timezone
        cached = store.get_measurement_cache(instrument, measurement)
        now = datetime.now(timezone.utc)
        if cached is not None:
            try:
                ts = datetime.fromisoformat(cached["timestamp"])
                age = (now - ts).total_seconds()
                if 0 <= age <= max_age_s:
                    return make_envelope("ok", data={
                        "instrument": instrument,
                        "measurement": measurement,
                        "value": cached["value"],
                        "unit": cached["unit"],
                        "age_s": round(age, 3),
                        "cached": True,
                        "timestamp": cached["timestamp"],
                    })
            except Exception:
                pass

        # cache 古い or なし
        # v0.7.0.1: refresh_if_stale=False なら実機 query を発生させない (安全側)
        if not refresh_if_stale:
            return make_envelope(
                "ok",
                data={
                    "instrument": instrument,
                    "measurement": measurement,
                    "value": None,
                    "stale": True,
                    "cached": cached is not None,
                    "cached_age_s": (
                        round((datetime.now(timezone.utc)
                               - datetime.fromisoformat(cached["timestamp"])).total_seconds(), 3)
                        if cached else None
                    ),
                    "note": (
                        "cache が古いまたは存在しないため None を返却。"
                        "実機から再取得するには refresh_if_stale=True を指定してください"
                    ),
                },
            )

        # refresh_if_stale=True → state_query から再取得
        item = session.definition.state_query.get(measurement)
        if item is None:
            return make_envelope(
                "error",
                errors=[make_error(
                    "not_found",
                    f"state_query.{measurement} が定義されていません",
                )],
            )
        from lab_executor.state_query import query_state_item
        r = await query_state_item(visa, session, measurement, item)
        if r.get("error"):
            return make_envelope(
                "error",
                errors=[make_error(
                    r["error"],
                    r.get("message", "read-back failed"),
                )],
            )
        value = r.get("value")
        try:
            store.upsert_measurement_cache(
                instrument, measurement, value, unit=item.unit,
            )
        except Exception:
            pass
        return make_envelope("ok", data={
            "instrument": instrument,
            "measurement": measurement,
            "value": value,
            "unit": item.unit,
            "age_s": 0.0,
            "cached": False,
        })
