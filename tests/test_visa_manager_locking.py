"""v0.4.0 リソース単位排他ロックのテスト"""
import asyncio
from unittest.mock import patch

import pytest


@pytest.mark.asyncio
async def test_per_resource_lock_exists():
    """同一 VisaManager 内で resource_name ごとのロックが生成される"""
    # pyvisa が無くてもテスト可能にするため、import を回避する形でモック
    with patch("lab_visa_mcp.visa_manager._PYVISA_AVAILABLE", True):
        from lab_visa_mcp.visa_manager import VisaManager
        mgr = VisaManager.__new__(VisaManager)
        mgr._rm = None
        mgr._locks = {}

        lock_a = mgr._get_lock("GPIB0::1::INSTR")
        lock_b = mgr._get_lock("GPIB0::2::INSTR")
        lock_a2 = mgr._get_lock("GPIB0::1::INSTR")

        assert isinstance(lock_a, asyncio.Lock)
        assert lock_a is lock_a2  # 同じ resource → 同じロック
        assert lock_a is not lock_b  # 異なる resource → 異なるロック


@pytest.mark.asyncio
async def test_same_resource_serializes():
    """同一 resource への複数 task は順次実行されることを確認"""
    with patch("lab_visa_mcp.visa_manager._PYVISA_AVAILABLE", True):
        from lab_visa_mcp.visa_manager import VisaManager
        mgr = VisaManager.__new__(VisaManager)
        mgr._rm = None
        mgr._locks = {}

        order = []

        async def task(label, delay):
            async with mgr._get_lock("R1"):
                order.append(f"{label}-start")
                await asyncio.sleep(delay)
                order.append(f"{label}-end")

        # 同じ resource で 2 つの task を並列起動
        await asyncio.gather(task("A", 0.05), task("B", 0.01))

        # A-end が B-start より先に来ているはず（直列化）
        assert order.index("A-end") < order.index("B-start")
