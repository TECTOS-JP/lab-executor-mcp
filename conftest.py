"""Project-level conftest:
1) ensure local `src/` on sys.path so tests run without
   `pip install -e .` (useful in network-restricted envs)
2) v2.14.2: provide a `job_store` fixture that auto-closes the
   SQLite connection on teardown (avoids Windows WAL file lock).
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if SRC.exists() and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@pytest.fixture
def job_store(tmp_path):
    """Yield a fresh `JobStore` rooted at a tmp path and close it on
    teardown so the SQLite WAL / SHM files release their handles
    (Windows pytest cleanup の詰まりを防ぐ)。
    """
    from lab_executor.job.store import JobStore
    store = JobStore(str(tmp_path / "jobs.db"))
    try:
        yield store
    finally:
        store.close()


@pytest.fixture
def seed_job():
    """A helper fixture that seeds a job row into a given store.
    Usage: `seed_job(store, job_id)`."""
    def _seed(store, job_id: str) -> None:
        store._connect().execute(
            "INSERT INTO jobs (job_id, owner, resource_name, status, "
            "current_step_index, created_at, updated_at) "
            "VALUES (?, '', '', 'completed', 0, "
            "'2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')",
            (job_id,),
        )
    return _seed
