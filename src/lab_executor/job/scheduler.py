"""
ResourceScheduler ── 同一 resource への Job を直列化するスケジューラ (v0.5.0.2)

設計方針 (外部レビュー v0.5.0.1 を受けて):
- 「VISA 操作単位の排他」(VisaManager の resource lock) だけでは Job が interleave する
  → このスケジューラが「Job 単位の排他」を実装する
- 同一 resource への Job は queued になり、前の Job 終端で次が起動
- queued 中は resource を占有しない (lock 保持なし)
- running / waiting 中は Job 終了まで resource を占有
- 停止系 (cancel_job / safe_shutdown) は queue を介さず実行中 Job への割り込み要求

将来 (v0.6.0 の Group / Map) に向けて、内部表現を `required_resources: list[str]` で持つ。
v0.5.x の単一 resource recipe job では `[resource_name]` の単一要素リスト。
複数 resource の場合は canonical (ソート済) 順序で lock 取得することで deadlock 回避。

queue_policy:
  - "queue" (デフォルト): 使用中なら queue 末尾に並ぶ
  - "reject_if_busy"   : 使用中なら即座に failed (queue_full / busy)
"""
from __future__ import annotations
import asyncio
import logging
from collections import deque
from typing import Awaitable, Callable, Literal

logger = logging.getLogger(__name__)


QueuePolicy = Literal["queue", "reject_if_busy"]


class ResourceBusyError(Exception):
    """reject_if_busy ポリシー時に resource が使用中の場合に raise"""

    def __init__(self, resources: list[str], blocking_job_id: str | None) -> None:
        super().__init__(
            f"resources {resources} are busy (blocked by job {blocking_job_id})"
        )
        self.resources = resources
        self.blocking_job_id = blocking_job_id


class ResourceScheduler:
    """
    resource 単位の queue と active 追跡を管理する。

    使い方:
        sched = ResourceScheduler()

        # job 投入
        immediate, blocking = await sched.enqueue(
            job_id="job_001",
            required_resources=["psu1"],
            queue_policy="queue",
        )
        # immediate: True なら即 running できる、False なら queue 末尾
        # blocking: 先頭で blocking している job_id (queued 時のみ意味あり)

        # job 終端時
        next_job_ids = await sched.on_terminal("job_001", ["psu1"])
        # 解放した resource に対応する次の queued job がある場合、
        # それらの job_id リストが返る (呼び出し側が running に遷移させる)
    """

    def __init__(self) -> None:
        # resource_name → 現在 active な job_id (1 個)
        self._active: dict[str, str] = {}
        # resource_name → 待ち順 (deque of job_id)
        self._queues: dict[str, deque[str]] = {}
        # job_id → 必要 resource リスト (canonical sorted)
        self._job_resources: dict[str, list[str]] = {}
        # 一括操作のための lock (asyncio 単一 event loop 想定なので簡単化)
        self._lock = asyncio.Lock()

    # ---------- public API ----------

    async def enqueue(
        self,
        job_id: str,
        required_resources: list[str],
        queue_policy: QueuePolicy = "queue",
    ) -> tuple[bool, str | None]:
        """
        Job を scheduler に登録する。

        返り値: (immediate_start, blocking_job_id)
          - immediate_start=True ならすべての resource が空いていて即 running 可能。
            **この時点で active にも登録されている** (race condition 回避のため)。
          - immediate_start=False なら queued 状態。blocking_job_id は先頭で blocking している
            既存 Job (複数の場合はそのうちの 1 つ)

        queue_policy="reject_if_busy" で busy なら ResourceBusyError を raise。
        """
        if not required_resources:
            # resource を必要としない Job (現在は想定外だが将来対応)
            return True, None

        canonical = sorted(set(required_resources))

        async with self._lock:
            # まずすべて空いているか確認
            blocking = self._find_blocker(canonical)

            if blocking is None:
                # 全 resource 空き → 即 running 可。
                # この時点で active に登録することで、後続 enqueue との競合を防ぐ。
                # on_running は no-op (確認) として動作する。
                self._job_resources[job_id] = canonical
                for r in canonical:
                    self._active[r] = job_id
                return True, None

            # busy
            if queue_policy == "reject_if_busy":
                raise ResourceBusyError(canonical, blocking)

            # queue 末尾へ
            self._job_resources[job_id] = canonical
            for r in canonical:
                self._queues.setdefault(r, deque()).append(job_id)
            return False, blocking

    async def on_running(self, job_id: str) -> None:
        """
        Job が running に遷移する直前に呼ぶ。

        v0.5.0.2: enqueue 時点で immediate=True なら active 登録済みのため、
        この呼び出しは「queue 経由 (待機後に起動可能となった)」場合のみ active への
        移動を行う。既に active なら no-op。
        """
        async with self._lock:
            resources = self._job_resources.get(job_id)
            if not resources:
                logger.warning("on_running: job %s の resource 情報がない", job_id)
                return
            for r in resources:
                if self._active.get(r) == job_id:
                    # 既に active (immediate=True の経路)。queue にはいない。
                    continue
                if r in self._active and self._active[r] != job_id:
                    logger.error(
                        "on_running: resource %s は既に job %s が active (要求 %s)",
                        r, self._active[r], job_id,
                    )
                    continue
                # queue 経由 → 先頭から自分を取り除き active へ
                q = self._queues.get(r)
                if q and q[0] == job_id:
                    q.popleft()
                self._active[r] = job_id

    async def on_terminal(self, job_id: str, required_resources: list[str]) -> list[str]:
        """
        Job が終端 (completed/failed/cancelled/timeout/interrupted) に達した時に呼ぶ。
        当該 Job の active 占有を解放し、各 resource の queue 先頭にいる Job のうち
        全 resource が空きになったものを返す。呼び出し側はそれらを running 遷移させる。
        """
        async with self._lock:
            for r in required_resources:
                if self._active.get(r) == job_id:
                    self._active.pop(r, None)
                # queue 先頭にいる可能性 (cancel された queued job)
                q = self._queues.get(r)
                if q and q[0] == job_id:
                    q.popleft()
            self._job_resources.pop(job_id, None)

            # 次に起動可能な Job を探す (queue 先頭で必要 resource が全て空)
            startable: list[str] = []
            seen: set[str] = set()
            for r in required_resources:
                q = self._queues.get(r)
                if not q:
                    continue
                candidate = q[0]
                if candidate in seen:
                    continue
                seen.add(candidate)
                if self._can_start(candidate):
                    startable.append(candidate)
            return startable

    async def cancel_queued(self, job_id: str) -> bool:
        """
        queued 状態の Job を queue から取り除く (cancel 用)。
        active な Job には触れない (それは Job 側の cancel フローで処理)。
        返り値: 取り除いた = True / 該当なし = False
        """
        async with self._lock:
            resources = self._job_resources.get(job_id)
            if not resources:
                return False
            removed_any = False
            for r in resources:
                q = self._queues.get(r)
                if q and job_id in q:
                    q.remove(job_id)
                    removed_any = True
            if removed_any:
                self._job_resources.pop(job_id, None)
            return removed_any

    async def get_scheduling_info(self, job_id: str) -> dict:
        """
        Job の scheduler 上のスケジューリング状態を返す (v0.5.0.4)。

        返り値の dict:
          - immediate_start: bool ── True なら enqueue 時点で全 resource 空いていて即 running 可
          - blocked_by_job: str | None ── queue 待ち中の場合の blocker job_id
          - queue_position: int ── queue 内位置 (0-indexed)、active 中は -1
          - resources: list[str] ── 必要 resource (canonical 順)
          - in_active: bool ── 現在 active マップに登録中
          - in_queue: bool ── 現在 queue に並んでいる
        """
        async with self._lock:
            resources = self._job_resources.get(job_id, [])
            in_active = any(self._active.get(r) == job_id for r in resources)
            in_queue = False
            position = -1
            blocking: str | None = None
            for r in resources:
                q = self._queues.get(r)
                if q and job_id in q:
                    in_queue = True
                    position = max(position, list(q).index(job_id))
                if blocking is None and not in_active:
                    blocker_active = self._active.get(r)
                    if blocker_active is not None and blocker_active != job_id:
                        blocking = blocker_active
                    elif q and q[0] != job_id:
                        blocking = q[0]
            return {
                "immediate_start": in_active,
                "blocked_by_job": blocking,
                "queue_position": position,
                "resources": list(resources),
                "in_active": in_active,
                "in_queue": in_queue,
            }

    async def get_queue_info(self, job_id: str) -> dict | None:
        """
        queued な Job の情報を返す。queue_position (0-indexed 最初の resource) と
        blocking_job_id を含む。queued でない場合は None。
        """
        async with self._lock:
            resources = self._job_resources.get(job_id)
            if not resources:
                return None
            # queue にいるかチェック
            in_queue = False
            position = -1
            blocking: str | None = None
            for r in resources:
                q = self._queues.get(r)
                if q and job_id in q:
                    in_queue = True
                    position = max(position, list(q).index(job_id))
                # 先頭の blocker (active or queue 先頭)
                if blocking is None:
                    blocking = self._active.get(r)
                    if blocking is None and q and q[0] != job_id:
                        blocking = q[0]
            if not in_queue:
                return None
            return {
                "queue_position": position,
                "blocking_job_id": blocking,
                "resources": list(resources),
            }

    def snapshot(self) -> dict:
        """テスト・デバッグ用に内部状態を返す (lock 取らない、概略のみ)"""
        return {
            "active": dict(self._active),
            "queues": {r: list(q) for r, q in self._queues.items()},
            "job_resources": dict(self._job_resources),
        }

    # ---------- internal ----------

    def _find_blocker(self, resources: list[str]) -> str | None:
        """指定 resource のいずれかが active or queue の先頭にいる場合、そのジョブ ID を返す。"""
        for r in resources:
            if r in self._active:
                return self._active[r]
            q = self._queues.get(r)
            if q:
                return q[0]
        return None

    def _can_start(self, job_id: str) -> bool:
        """job_id が要求する全 resource が空 (or 自分が queue 先頭) なら True"""
        resources = self._job_resources.get(job_id, [])
        for r in resources:
            if r in self._active and self._active[r] != job_id:
                return False
            q = self._queues.get(r)
            if q and q[0] != job_id:
                return False
        return True
