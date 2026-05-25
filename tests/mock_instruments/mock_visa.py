"""
pyvisa をモックして実機なしでテストするためのヘルパー。
"""
from unittest.mock import MagicMock, patch


class MockResource:
    def __init__(self, idn_response: str = "MOCK,INSTRUMENT,000001,V1.00"):
        self.timeout = 5000
        self.read_termination = "\n"
        self.write_termination = "\n"
        self._idn = idn_response
        self._responses: dict[str, str] = {"*IDN?": idn_response}

    def add_response(self, command: str, response: str) -> None:
        self._responses[command] = response

    def query(self, command: str) -> str:
        if command in self._responses:
            return self._responses[command]
        raise Exception(f"モック機器に '{command}' の応答が登録されていません。")

    def write(self, command: str) -> None:
        pass  # write は常に成功

    def close(self) -> None:
        pass


class MockResourceManager:
    def __init__(self, resources: dict[str, MockResource] | None = None):
        self._resources = resources or {}

    def list_resources(self, query: str = "?*::INSTR") -> tuple[str, ...]:
        return tuple(self._resources.keys())

    def open_resource(self, resource_name: str) -> MockResource:
        if resource_name not in self._resources:
            raise Exception(f"モックリソース '{resource_name}' が登録されていません。")
        return self._resources[resource_name]

    def close(self) -> None:
        pass

    @property
    def visalib(self):
        return "MockVISALib"


def make_mock_rm(*instrument_specs: tuple[str, str]) -> MockResourceManager:
    """
    (resource_name, idn_response) のタプルリストからモック ResourceManager を生成する。
    例: make_mock_rm(("GPIB0::1::INSTR", "TEKTRONIX,TDS 210,C001,V1.00"))
    """
    resources = {
        name: MockResource(idn)
        for name, idn in instrument_specs
    }
    return MockResourceManager(resources)
