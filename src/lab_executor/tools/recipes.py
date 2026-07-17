"""Recipe 関連の MCP ツール (v0.3.0)"""
from __future__ import annotations
from typing import TYPE_CHECKING
from fastmcp import FastMCP
from lab_executor.recipe_executor import execute_recipe as _execute_recipe

if TYPE_CHECKING:
    # v1.11: backend layer top-level import 排除 (型ヒントのみ)
    from visa_mcp.session_manager import SessionManager


def register_tools(mcp: FastMCP, session_mgr: SessionManager) -> None:

    @mcp.tool()
    async def list_recipes(resource_name: str) -> dict:
        """
        指定機器で利用可能な recipe (典型ワークフロー) を一覧する。
        recipe は複数コマンドの安全な順序を YAML で宣言したもの。

        resource_name: VISA リソース文字列
        """
        session = session_mgr.get_session(resource_name)
        if session is None or session.definition is None:
            return {
                "success": False,
                "error": "SessionNotFound",
                "message": f"{resource_name} は未識別、または YAML 定義がありません。",
            }
        items = []
        for name, r in session.definition.recipes.items():
            items.append({
                "name": name,
                "description": r.description,
                "parameters": [p.model_dump() for p in r.parameters],
                "step_count": len(r.steps),
                "commands_used": [s.command for s in r.steps],
            })
        return {"success": True, "data": {"recipes": items, "count": len(items)}}

    @mcp.tool()
    async def execute_recipe(
        resource_name: str,
        recipe_name: str,
        parameters: dict = {},
        override_safety: bool = False,
        override_reason: str = "",
    ) -> dict:
        """
        指定機器で recipe を実行する。recipe は YAML に定義された複数コマンドの
        シーケンスで、安全な順序が保証される (例: OVP/OCP 設定後に出力 ON)。

        resource_name: VISA リソース文字列
        recipe_name: YAML 定義の recipe キー (list_recipes で確認可能)
        parameters: recipe パラメータ辞書 (例: {"target_v": 5.0})
        override_safety: 安全警告を無視 (advisory モード時のみ、理由必須)
        override_reason: override 理由
        """
        session = session_mgr.get_session(resource_name)
        if session is None:
            return {
                "success": False,
                "error": "SessionNotFound",
                "message": f"{resource_name} は未識別です。",
            }

        result = await _execute_recipe(
            session_mgr._visa, session, recipe_name, parameters,
            override_safety=override_safety, override_reason=override_reason,
            session_resolver=session_mgr.get_session,
        )
        return result
