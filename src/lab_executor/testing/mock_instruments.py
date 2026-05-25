"""v0.9.0: Mock instruments for benchmark / unit tests.

VISA への依存ゼロで、実機なしに動作するシナリオベース mock。
`MockVisaManager` は `VisaManager` と同じ `query` / `write` / `list_resources`
を提供し、JobManager にそのまま渡して使える。

シナリオは YAML/JSON の dict で与え、resource_name × command 単位で挙動を
制御する。
"""
from __future__ import annotations
import asyncio
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, Literal


# ============================================================
# Mock 例外 (VisaTimeoutError と意味的に等価)
# ============================================================


class MockTimeoutError(TimeoutError):
    """mock 機器が timeout シナリオで発生させる擬似 VISA timeout"""


class MockProtocolError(Exception):
    """mock 機器が protocol エラーシナリオで発生させる擬似プロトコルエラー"""


# ============================================================
# シナリオ DSL
# ============================================================


MockMode = Literal[
    "constant",       # 常に同じ値を返す
    "echo",           # 直前に書き込まれた値を返す (PSU の set/query 連動用)
    "stable",         # 常に target_value ± noise
    "stable_after",   # initial_value → final_value、stable_after_s 秒で到達
    "drifting",       # 線形ドリフト
    "timeout",        # query 時に MockTimeoutError
    "flaky",          # 最初 N 回 timeout、その後成功
    "verify_mismatch",  # write した値 + actual_offset を query で返す
    "raise_protocol",  # MockProtocolError を投げる
]


@dataclass
class InstrumentScenario:
    """1 つの resource_name に紐づく mock 挙動。

    command_pattern は正規表現で write/query コマンドにマッチする。
    複数 scenario を持つ場合は最初にマッチしたものが採用される。
    """
    command_pattern: str = ".*"           # 正規表現
    mode: MockMode = "constant"
    value: Any = "0.0"                    # constant / echo の戻り値
    initial_value: float | None = None
    final_value: float | None = None
    target_value: float | None = None
    stable_after_s: float = 30.0
    noise: float = 0.0
    drift_per_s: float = 0.1
    timeout_after_calls: int = 0
    actual_offset: float = 0.0            # verify_mismatch 用
    response_format: str = "{value}"      # 例: "NTKC{value:+010.4E}+0"

    # 内部状態
    _call_count: int = 0
    _last_write: str | None = None
    _last_write_value: float | None = None
    _start_time: float = field(default_factory=time.monotonic)

    def matches(self, command: str) -> bool:
        try:
            return re.fullmatch(self.command_pattern, command) is not None \
                or re.search(self.command_pattern, command) is not None
        except re.error:
            return self.command_pattern == command

    def _format(self, val: float | str) -> str:
        if isinstance(val, (int, float)):
            try:
                return self.response_format.format(value=val)
            except (KeyError, IndexError, ValueError):
                return str(val)
        return str(val)

    def on_query(self, command: str) -> str:
        self._call_count += 1
        if self.mode == "timeout":
            raise MockTimeoutError(
                f"mock timeout on command={command!r}"
            )
        if self.mode == "flaky":
            if self._call_count <= self.timeout_after_calls:
                raise MockTimeoutError(
                    f"mock flaky timeout (call {self._call_count}/"
                    f"{self.timeout_after_calls})"
                )
            base = (self.value if not isinstance(self.value, (int, float))
                    else float(self.value))
            return self._format(base)
        if self.mode == "raise_protocol":
            raise MockProtocolError(
                f"mock protocol error on command={command!r}"
            )
        if self.mode == "constant":
            return self._format(self.value)
        if self.mode == "echo":
            return self._format(
                self._last_write_value
                if self._last_write_value is not None
                else (float(self.value) if isinstance(self.value, (int, float))
                      else self.value)
            )
        if self.mode == "stable":
            tv = (self.target_value
                  if self.target_value is not None else float(self.value))
            jitter = (random.uniform(-self.noise, self.noise)
                      if self.noise > 0 else 0.0)
            return self._format(tv + jitter)
        if self.mode == "stable_after":
            elapsed = time.monotonic() - self._start_time
            iv = self.initial_value or 0.0
            fv = self.final_value if self.final_value is not None else iv
            if elapsed >= self.stable_after_s:
                cur = fv
            else:
                frac = max(0.0, min(1.0, elapsed / max(self.stable_after_s, 1e-9)))
                cur = iv + (fv - iv) * frac
            jitter = (random.uniform(-self.noise, self.noise)
                      if self.noise > 0 else 0.0)
            return self._format(cur + jitter)
        if self.mode == "drifting":
            elapsed = time.monotonic() - self._start_time
            iv = self.initial_value or 0.0
            return self._format(iv + self.drift_per_s * elapsed)
        if self.mode == "verify_mismatch":
            base = (self._last_write_value
                    if self._last_write_value is not None
                    else (float(self.value) if isinstance(self.value, (int, float))
                          else 0.0))
            return self._format(base + self.actual_offset)
        return self._format(self.value)

    def on_write(self, command: str) -> None:
        self._call_count += 1
        self._last_write = command
        # 数値らしき部分を抽出して echo / verify_mismatch 用に保存
        m = re.search(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?", command)
        if m:
            try:
                self._last_write_value = float(m.group(0))
            except ValueError:
                pass
        if self.mode == "timeout":
            raise MockTimeoutError(f"mock timeout on write {command!r}")
        if self.mode == "raise_protocol":
            raise MockProtocolError(f"mock protocol on write {command!r}")
        if self.mode == "flaky" and self._call_count <= self.timeout_after_calls:
            raise MockTimeoutError(
                f"mock flaky timeout on write (call {self._call_count})"
            )


# ============================================================
# Mock VisaManager
# ============================================================


class MockVisaManager:
    """VisaManager 互換の async API を持つ mock。

    内部に { resource_name: [InstrumentScenario, ...] } を持ち、
    query/write 時にマッチした scenario を呼ぶ。
    マッチしない場合は constant scenario (value="0.0") にフォールバック。
    """

    def __init__(self) -> None:
        self._scenarios: dict[str, list[InstrumentScenario]] = {}
        # 観察用: 全 I/O 履歴
        self.io_log: list[dict] = []

    # ---- 設定 ----

    def register(
        self, resource_name: str, *scenarios: InstrumentScenario,
    ) -> None:
        """resource に scenario を 1 つ以上紐づける (先頭から走査)"""
        self._scenarios.setdefault(resource_name, []).extend(scenarios)

    def reset(self) -> None:
        for sl in self._scenarios.values():
            for s in sl:
                s._call_count = 0
                s._last_write = None
                s._last_write_value = None
                s._start_time = time.monotonic()
        self.io_log.clear()

    def _scenario_for(
        self, resource_name: str, command: str,
    ) -> InstrumentScenario:
        scs = self._scenarios.get(resource_name, [])
        for s in scs:
            if s.matches(command):
                return s
        # フォールバック (失敗を出さない constant)
        return InstrumentScenario(mode="constant", value="0.0")

    # ---- VisaManager 互換 API ----

    async def list_resources(self) -> list[str]:
        return list(self._scenarios.keys())

    async def query(
        self,
        resource_name: str,
        command: str,
        timeout_ms: int = 5000,
        read_termination: str = "\n",
        write_termination: str = "\n",
    ) -> str:
        sc = self._scenario_for(resource_name, command)
        # 短時間 yield (実機 polling の sleep を模擬する程度)
        await asyncio.sleep(0)
        try:
            resp = sc.on_query(command)
        except MockTimeoutError:
            self.io_log.append({
                "type": "query", "resource": resource_name,
                "command": command, "result": "timeout",
            })
            # VisaTimeoutError 同等のエラーを上位に渡す
            from visa_mcp.visa_manager import VisaTimeoutError
            raise VisaTimeoutError(
                f"mock timeout on {resource_name} command={command!r}",
            )
        self.io_log.append({
            "type": "query", "resource": resource_name,
            "command": command, "response": resp,
        })
        return resp

    async def write(
        self,
        resource_name: str,
        command: str,
        timeout_ms: int = 5000,
        read_termination: str = "\n",
        write_termination: str = "\n",
    ) -> None:
        sc = self._scenario_for(resource_name, command)
        await asyncio.sleep(0)
        try:
            sc.on_write(command)
        except MockTimeoutError:
            self.io_log.append({
                "type": "write", "resource": resource_name,
                "command": command, "result": "timeout",
            })
            from visa_mcp.visa_manager import VisaTimeoutError
            raise VisaTimeoutError(
                f"mock timeout on write {resource_name} command={command!r}",
            )
        self.io_log.append({
            "type": "write", "resource": resource_name,
            "command": command,
        })


# ============================================================
# Scenario loader (YAML / dict)
# ============================================================


def scenarios_from_dict(data: dict) -> dict[str, list[InstrumentScenario]]:
    """YAML 形式 `{ resource: [{...}, ...] }` を scenarios に変換"""
    out: dict[str, list[InstrumentScenario]] = {}
    for res, scs in data.items():
        items: list[InstrumentScenario] = []
        if isinstance(scs, dict):
            scs = [scs]
        for s in (scs or []):
            items.append(InstrumentScenario(**s))
        out[res] = items
    return out
