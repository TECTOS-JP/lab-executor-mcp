"""
v0.6.1 BarrierCoordinator ── Map/Group Job 内の target 間同期点

設計 (実装方針推奨 5 点):
1. Barrier は Group/Map executor の同期機構 (target-local Plan step ではなく)
2. Barrier 待ち中は target-level resource lock を解放 (deadlock 回避)
3. failure_policy=continue では失敗 target を barrier 対象から除外
4. stagger は CommandStep の field (target_index * stagger_ms 遅延)
5. progress に barrier_name / arrived / waiting_for / next_target_id 等

設計詳細:
- barrier_key = (name, step_index) で識別 (同 name でも step_index 違いは別物)
- 全 target が arrive、または timeout / cancel で抜ける
- arrive 時点で対象 target 数を確定 (失敗・skipped target は事前に exclude する API あり)
"""
from __future__ import annotations
import asyncio
import logging
import time
from typing import Callable

logger = logging.getLogger(__name__)


class BarrierState:
    """1 つの barrier (name, step_index) の到達状況"""

    def __init__(self, name: str, step_index: int) -> None:
        self.name = name
        self.step_index = step_index
        # 対象 target_id のセット (失敗等で除外された target は含まれない)
        self._participants: set[str] = set()
        # 到達済み target_id のセット
        self.arrived: set[str] = set()
        # 全到達 / timeout / cancel で立つ Event
        self.event: asyncio.Event = asyncio.Event()
        # timeout / cancel で抜けた場合の理由
        self.aborted_reason: str | None = None
        self.created_at = time.monotonic()

    @property
    def key(self) -> tuple[str, int]:
        return (self.name, self.step_index)

    def add_participant(self, target_id: str) -> None:
        self._participants.add(target_id)

    def exclude_participant(self, target_id: str) -> None:
        """失敗等で対象から除外。除外後に全到達条件を満たしたら event.set"""
        self._participants.discard(target_id)
        self.arrived.discard(target_id)
        self._check_complete()

    def total_expected(self) -> int:
        return len(self._participants)

    def waiting_for_count(self) -> int:
        return max(0, len(self._participants) - len(self.arrived))

    def waiting_target_ids(self) -> list[str]:
        return sorted(self._participants - self.arrived)

    def mark_arrived(self, target_id: str) -> None:
        if target_id in self._participants:
            self.arrived.add(target_id)
            self._check_complete()

    def _check_complete(self) -> None:
        if self._participants and self.arrived >= self._participants:
            self.event.set()

    def abort(self, reason: str) -> None:
        """timeout / cancel 等で barrier 全体を強制終了させる"""
        if self.aborted_reason is None:
            self.aborted_reason = reason
            self.event.set()


class BarrierCoordinator:
    """Map/Group Job 内の barrier 状態を集約管理する。

    使い方:
      coord = BarrierCoordinator()
      coord.register_targets(["t1", "t2", "t3"])
      # 各 target が barrier に到達したら:
      await coord.arrive(barrier_name="b1", step_index=2, target_id="t1")
      # target が途中で失敗したら:
      coord.exclude_target("t2")

    Barrier 対象 target の決定規則 (v0.6.1.1 明文化):
      - register_targets() で渡された全 target が初期 participants
      - exclude_target(tid) で動的除外 (失敗 / cancelled target)
      - 各 barrier (name, step_index) の participants は **arrive 時点で
        その時の non-excluded set がスナップショットされる**
      - barrier を持たない target が混在する場合: そのような target は
        arrive() を呼ばないので、他 target が waiting_for に残し続け、
        最終的に barrier.timeout_s で abort される。
        → **同一 Map Job 内では全 target が同じ barrier_key 集合を持つことを推奨**
        (v0.6.1 では validation は行わない、運用ルール)

    abort 後の挙動:
      - timeout / cancel で abort された barrier に late arrival した target は
        即座に success=False, error=aborted_reason で return する
        (新たな wait に入らない)
    """

    def __init__(self) -> None:
        # (name, step_index) → BarrierState
        self._barriers: dict[tuple[str, int], BarrierState] = {}
        # 登録済みの全 target_id (初期参加者)
        self._all_targets: set[str] = set()
        # 除外済み (失敗 target など)
        self._excluded: set[str] = set()
        # 各 barrier の active 統計 (debug)
        self._progress: dict | None = None
        self._lock = asyncio.Lock()

    def register_targets(self, target_ids: list[str]) -> None:
        """Job 開始時に呼ぶ。全 target_id を participants として登録"""
        self._all_targets = set(target_ids)

    def exclude_target(self, target_id: str) -> None:
        """target が失敗 / skipped になった場合、以降の barrier から除外。

        既存の active barrier からも即時除外し、残り participants が全到達なら
        event.set される (deadlock 回避)。
        """
        self._excluded.add(target_id)
        for state in self._barriers.values():
            state.exclude_participant(target_id)

    async def _get_or_create(
        self, name: str, step_index: int,
    ) -> BarrierState:
        async with self._lock:
            key = (name, step_index)
            if key not in self._barriers:
                state = BarrierState(name=name, step_index=step_index)
                # 現時点で除外されていない全 target を participants として登録
                # (barrier に最初に到達した target が他の到達を待つ前提)
                for tid in self._all_targets:
                    if tid not in self._excluded:
                        state.add_participant(tid)
                self._barriers[key] = state
            return self._barriers[key]

    async def arrive(
        self,
        name: str,
        step_index: int,
        target_id: str,
        timeout_s: float,
        cancel_check: Callable[[], str | None] | None = None,
    ) -> dict:
        """target_id が barrier (name, step_index) に到達したことを通知し、
        全 target 到達 / timeout / cancel まで待機する。

        返り値:
          {
            "success": bool,
            "barrier_name": name,
            "step_index": step_index,
            "total_expected": int,
            "arrived": int,
            "waited_s": float,
            "error": "timeout" | "cancel" | None,
            "interrupted_by_timeout"?: bool,
            "interrupted_by_cancel"?: bool,
          }
        """
        state = await self._get_or_create(name, step_index)
        # v0.6.1.1: 既に abort 済みの barrier への late arrival は即失敗で返す
        # (新たな wait に入らない、deadlock 防止)
        if state.aborted_reason is not None:
            return {
                "success": False,
                "barrier_name": name,
                "step_index": step_index,
                "total_expected": state.total_expected(),
                "arrived": len(state.arrived),
                "waited_s": 0.0,
                "error": state.aborted_reason,
                ("interrupted_by_" + state.aborted_reason): True,
                "late_arrival": True,
            }
        state.mark_arrived(target_id)
        t_start = time.monotonic()
        deadline = t_start + timeout_s

        # slice wait で cancel/timeout に即応
        from lab_executor.polling_executor import POLL_SLEEP_SLICE_S
        while not state.event.is_set():
            # 親 Job cancel
            if cancel_check is not None:
                r = cancel_check()
                if r:
                    state.abort(r)
                    break
            # timeout
            now = time.monotonic()
            if now >= deadline:
                state.abort("timeout")
                break
            remaining = deadline - now
            try:
                await asyncio.wait_for(
                    asyncio.shield(state.event.wait()),
                    timeout=min(POLL_SLEEP_SLICE_S, remaining),
                )
            except asyncio.TimeoutError:
                continue

        waited = time.monotonic() - t_start
        if state.aborted_reason:
            return {
                "success": False,
                "barrier_name": name,
                "step_index": step_index,
                "total_expected": state.total_expected(),
                "arrived": len(state.arrived),
                "waited_for": state.waiting_target_ids(),
                "waited_s": waited,
                "error": state.aborted_reason,
                ("interrupted_by_" + state.aborted_reason): True,
            }
        return {
            "success": True,
            "barrier_name": name,
            "step_index": step_index,
            "total_expected": state.total_expected(),
            "arrived": len(state.arrived),
            "waited_s": waited,
        }

    def snapshot(self) -> dict:
        """progress 公開用のスナップショット"""
        return {
            "type": "barrier_coordinator",
            "barriers": {
                f"{k[0]}#{k[1]}": {
                    "name": v.name,
                    "step_index": v.step_index,
                    "arrived": len(v.arrived),
                    "total_expected": v.total_expected(),
                    "waiting_for": v.waiting_target_ids(),
                    "aborted": v.aborted_reason,
                }
                for k, v in self._barriers.items()
            },
            "excluded_targets": sorted(self._excluded),
        }

    def current_barrier_progress(self) -> dict | None:
        """現在 active な (まだ全到達していない / abort もしていない) barrier の
        うちもっとも新しいものの progress を返す。なければ None。
        """
        active = [
            s for s in self._barriers.values()
            if not s.event.is_set() and s.aborted_reason is None
        ]
        if not active:
            return None
        # 一番新しい barrier
        s = max(active, key=lambda x: x.created_at)
        return {
            "type": "barrier",
            "barrier_name": s.name,
            "step_index": s.step_index,
            "arrived": len(s.arrived),
            "total_expected": s.total_expected(),
            "waiting_for": s.waiting_target_ids(),
            "elapsed_s": time.monotonic() - s.created_at,
        }
