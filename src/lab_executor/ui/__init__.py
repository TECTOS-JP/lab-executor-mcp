"""lab-executor Web UI (M1: 読み取り専用モニタ)。

v2.19.0+ で追加した Web UI の M1 実装。`lab-executor ui` サブコマンドから
localhost に読み取り専用の実験モニタを起動する。

設計の絶対制約:
- 実験ランタイム (server.py / tools / serve 経路 / MCP ツール面) には一切触れない。
- SQLite への接続は必ず read-only (``file:...?mode=ro`` + ``PRAGMA query_only=ON``)。
  ``JobStore`` はコンストラクタで schema 書き込みを行うため UI からはインスタンス化しない。
- severity / phase / outcome / timeline の正規化は ``lab_executor.observation`` の
  既存純関数を import して使い、再実装しない (AI と人間が同じビューを見る設計)。

fastapi / uvicorn / jinja2 は optional-dependencies ``[ui]`` に置き、cli 側で
遅延 import する。本パッケージの import 自体は軽量に保つため、ここでは
FastAPI 依存モジュールを import しない。
"""
from __future__ import annotations

UI_VERSION = "m3"
