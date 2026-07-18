"""BEF-1 backend contract and conformance-kit self tests."""
import pytest

from lab_executor.backends import MockBackend
from lab_executor.testing.backend_conformance import assert_backend_contract


@pytest.mark.asyncio
async def test_bundled_mock_backend_conforms():
    backend = MockBackend()

    resolved = await assert_backend_contract(
        backend, sample_resource="MOCK::CONFORMANCE",
    )

    assert resolved is backend


@pytest.mark.asyncio
async def test_conformance_kit_rejects_wrong_query_result_type():
    class BrokenBackend:
        backend_id = "broken"

        async def list_resources(self):
            return ["BROKEN::1"]

        async def query(
            self,
            resource_name,
            command,
            timeout_ms=5000,
            read_termination="\n",
            write_termination="\n",
        ):
            return 123

        async def write(
            self,
            resource_name,
            command,
            timeout_ms=5000,
            read_termination="\n",
            write_termination="\n",
        ):
            return None

    with pytest.raises(AssertionError, match="query must return str"):
        await assert_backend_contract(
            BrokenBackend, sample_resource="BROKEN::1",
        )
