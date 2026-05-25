"""ResourceScheduler 単体テスト (v0.5.0.2)"""
import pytest

from lab_executor.job.scheduler import ResourceScheduler, ResourceBusyError


@pytest.mark.asyncio
async def test_first_job_immediate():
    s = ResourceScheduler()
    immediate, blocking = await s.enqueue("job_1", ["psu1"])
    assert immediate is True
    assert blocking is None


@pytest.mark.asyncio
async def test_second_job_queued():
    s = ResourceScheduler()
    await s.enqueue("job_1", ["psu1"])
    await s.on_running("job_1")
    immediate, blocking = await s.enqueue("job_2", ["psu1"])
    assert immediate is False
    assert blocking == "job_1"


@pytest.mark.asyncio
async def test_reject_if_busy():
    s = ResourceScheduler()
    await s.enqueue("job_1", ["psu1"])
    await s.on_running("job_1")
    with pytest.raises(ResourceBusyError) as exc:
        await s.enqueue("job_2", ["psu1"], queue_policy="reject_if_busy")
    assert "psu1" in exc.value.resources
    assert exc.value.blocking_job_id == "job_1"


@pytest.mark.asyncio
async def test_on_terminal_releases_and_returns_next():
    s = ResourceScheduler()
    await s.enqueue("job_1", ["psu1"])
    await s.on_running("job_1")
    await s.enqueue("job_2", ["psu1"])  # queued

    next_jobs = await s.on_terminal("job_1", ["psu1"])
    assert next_jobs == ["job_2"]
    # job_2 is now eligible
    await s.on_running("job_2")
    snap = s.snapshot()
    assert snap["active"]["psu1"] == "job_2"


@pytest.mark.asyncio
async def test_different_resources_no_blocking():
    """異なる resource は並列で immediate=True"""
    s = ResourceScheduler()
    a, _ = await s.enqueue("job_1", ["psu1"])
    b, _ = await s.enqueue("job_2", ["psu2"])
    assert a is True
    assert b is True


@pytest.mark.asyncio
async def test_cancel_queued():
    s = ResourceScheduler()
    await s.enqueue("job_1", ["psu1"])
    await s.on_running("job_1")
    await s.enqueue("job_2", ["psu1"])

    removed = await s.cancel_queued("job_2")
    assert removed is True
    # 残っているのは job_1 のみ。job_1 終端で次に起動するものはない
    next_jobs = await s.on_terminal("job_1", ["psu1"])
    assert next_jobs == []


@pytest.mark.asyncio
async def test_cancel_queued_nonexistent():
    s = ResourceScheduler()
    removed = await s.cancel_queued("nonexistent")
    assert removed is False


@pytest.mark.asyncio
async def test_get_queue_info():
    s = ResourceScheduler()
    await s.enqueue("job_a", ["psu1"])
    await s.on_running("job_a")
    await s.enqueue("job_b", ["psu1"])
    await s.enqueue("job_c", ["psu1"])

    info_b = await s.get_queue_info("job_b")
    assert info_b is not None
    assert info_b["blocking_job_id"] == "job_a"

    info_a = await s.get_queue_info("job_a")
    # job_a は active なので queue にいない → None
    assert info_a is None


@pytest.mark.asyncio
async def test_multi_resource_canonical_order():
    """複数 resource の場合、canonical (sorted) 順序で取得"""
    s = ResourceScheduler()
    await s.enqueue("job_1", ["psu2", "psu1"])  # sorted: [psu1, psu2]
    await s.on_running("job_1")
    # 1 つだけ被る Job
    immediate, blocking = await s.enqueue("job_2", ["psu1"])
    assert immediate is False
    assert blocking == "job_1"


@pytest.mark.asyncio
async def test_multi_resource_release():
    """複数 resource Job の終端で両方解放"""
    s = ResourceScheduler()
    await s.enqueue("job_1", ["psu1", "psu2"])
    await s.on_running("job_1")
    await s.enqueue("job_2", ["psu1"])  # queued
    await s.enqueue("job_3", ["psu2"])  # queued

    next_jobs = await s.on_terminal("job_1", ["psu1", "psu2"])
    assert set(next_jobs) == {"job_2", "job_3"}
