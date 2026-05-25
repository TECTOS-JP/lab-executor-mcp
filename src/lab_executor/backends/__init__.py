"""lab-executor backend layer (v2.0): Protocol + MockBackend.

PyVisaBackend は visa-mcp 側に残る。lab-executor 側 runtime は
InstrumentBackend Protocol を通じて backend を扱う。
"""
from lab_executor.backends.base import InstrumentBackend
from lab_executor.backends.mock_backend import MockBackend

__all__ = ["InstrumentBackend", "MockBackend"]
