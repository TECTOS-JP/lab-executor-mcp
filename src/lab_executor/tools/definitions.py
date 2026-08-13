"""Definition-driven tools that need no transport of their own.

``list_commands`` has always been listed in the stability matrix under
"Core / instrument", but only ``visa-mcp`` ever registered it. Every other
backend therefore had no way to say which of an instrument's commands operate
it and which only read from it — the one distinction an operator must never be
left to infer. Nothing in it is VISA-specific: it reads the bound definition.

Registered from ``compose_server`` only. ``visa-mcp`` builds its own server and
registers its own copy from ``lab_visa_mcp.tools.discovery``; registering here
as well would collide on the tool name, since its ``tools.info`` is a re-export
of this package's.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fastmcp import FastMCP


def register_tools(mcp: "FastMCP", session_mgr: Any) -> None:
    """Register the definition-driven instrument tools."""

    @mcp.tool()
    async def list_commands(resource_name: str) -> dict:
        """識別済み機器の利用可能なコマンド一覧と説明を返す。

        execute_named_command で使用可能な command_name を確認するために使う。
        resource_name: リソース文字列
        """
        session = session_mgr.get_session(resource_name)
        if session is None:
            return {
                "success": False,
                "error": "SessionNotFound",
                "message": (
                    f"{resource_name} はまだ識別されていません。"
                    "機器定義が結び付いているか確認してください。"
                ),
            }
        definition = getattr(session, "definition", None)
        if definition is None:
            return {
                "success": False,
                "error": "NoDefinitionFound",
                "message": f"{resource_name} の YAML 定義が見つかりませんでした。",
            }

        commands: dict[str, Any] = {}
        for name, command in definition.commands.items():
            commands[name] = {
                "description": command.description,
                # The caller decides how to present an operation versus a
                # reading from this; it is the reason the tool exists.
                "type": command.type,
                # Everything the definition says about an argument travels
                # with it. A caller that has to build the argument list -- an
                # agent composing a plan, or a person filling in a form -- can
                # then offer the allowed values instead of finding out from a
                # validation failure which arguments existed.
                "parameters": [
                    {
                        "name": p.name,
                        "type": p.type,
                        "required": p.required,
                        "description": p.description,
                        "range": list(p.range) if p.range else None,
                        "choices": list(p.choices) if p.choices else None,
                        "default": p.default,
                    }
                    for p in command.parameters
                ],
                "returns": {
                    "type": command.returns.type,
                    "unit": command.returns.unit,
                },
            }
        return {
            "success": True,
            "data": {
                "resource_name": resource_name,
                "instrument": (
                    f"{definition.metadata.manufacturer} {definition.metadata.model}"
                ),
                "commands": commands,
            },
        }


__all__ = ["register_tools"]
