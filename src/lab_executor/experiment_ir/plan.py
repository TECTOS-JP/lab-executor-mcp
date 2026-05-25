"""
Experiment IR の Plan 型定義 (v0.5.0)

Plan = 「実験手順 1 単位」の正規表現。
Recipe / Group / DSL のすべてが一旦 Plan に変換され、Job executor が実行する。

将来 (v0.8.0 DSL) では LLM が直接 Plan を組み立てて submit する経路を提供。
"""
from __future__ import annotations
from typing import Any

from pydantic import BaseModel, Field

from lab_executor.experiment_ir.step import Step


class Plan(BaseModel):
    """
    実験手順 1 単位。

    - `name`: 識別用 (description より) / 監査ログにも記録される
    - `parameters`: Plan 全体に渡される変数辞書 ($var で参照される)
    - `steps`: 順次実行されるステップのリスト
    - `resource_hint`: 主に使用するリソース名 (Job manager の lock 取得に使用、optional)
    - `required_resources`: v0.5.1 ── この Plan が排他占有する必要がある instrument
      (resource_name または alias) のリスト。canonical sorted 順。
      polling 系 wait step が別 instrument を参照する場合もここに含める。
    - `metadata`: 任意の追加情報 (生成元 recipe 名、生成時刻 等)
    """
    name: str = ""
    parameters: dict[str, Any] = Field(default_factory=dict)
    steps: list[Step] = Field(default_factory=list)
    resource_hint: str | None = None
    required_resources: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def step_count(self) -> int:
        return len(self.steps)
