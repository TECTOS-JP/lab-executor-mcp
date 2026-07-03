"""
実機を使った統合テスト。

実行方法:
    pytest -m hardware tests/test_hardware_integration.py -v

必要機器:
    USB0::0x0B3E::0x1029::ZM000463::INSTR  — Kikusui PMX35-3A (USB/SCPI)
    GPIB0::2::INSTR                          — Yokogawa 7563 (GPIB/非SCPI)

安全方針:
    - 出力 ON は行わない (query コマンドのみ)
    - v0.4.1 の safety 検出テストは実際のコマンド送信なし
"""
import os
import pytest
from pathlib import Path

pytest.importorskip("pyvisa")

# 実機必須テスト: Kikusui PMX 電源および Yokogawa 計測器の実機が接続されていないと
# 失敗する。CI やローカルの通常実行では environment 変数 LAB_EXECUTOR_HW_TESTS=1 が
# 設定されている場合のみ実行する。
pytestmark = [
    pytest.mark.hardware,
    pytest.mark.skipif(
        os.environ.get("LAB_EXECUTOR_HW_TESTS") != "1",
        reason=(
            "実機必須テスト (Kikusui PMX 電源 / Yokogawa 計測器)。"
            "実行するには環境変数 LAB_EXECUTOR_HW_TESTS=1 を設定すること。"
        ),
    ),
]

INSTRUMENTS_DIR = Path(__file__).parent.parent / "examples" / "instruments"
PMX_RESOURCE = "USB0::0x0B3E::0x1029::ZM000463::INSTR"
YOK_RESOURCE = "GPIB0::2::INSTR"


# ---------------------------------------------------------------------------
# フィクスチャ
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
async def session_mgr():
    from visa_mcp.visa_manager import VisaManager
    from lab_executor.instrument_registry import InstrumentRegistry
    from visa_mcp.session_manager import SessionManager

    visa = VisaManager()
    registry = InstrumentRegistry(INSTRUMENTS_DIR)
    sm = SessionManager(visa, registry)
    yield sm


# ---------------------------------------------------------------------------
# 1. YAML 定義ロード確認
# ---------------------------------------------------------------------------

def test_definitions_loaded(session_mgr):
    """examples/instruments/ の YAML が 2 件以上ロードされている"""
    defs = session_mgr._registry.list_definitions()
    assert len(defs) >= 2, f"定義が少なすぎる: {defs}"
    manufacturers = {d["manufacturer"] for d in defs}
    assert "Kikusui" in manufacturers
    assert "Yokogawa" in manufacturers


# ---------------------------------------------------------------------------
# 2. PMX35-3A: *IDN? 識別
# ---------------------------------------------------------------------------

async def test_pmx_identify(session_mgr):
    """PMX35-3A を *IDN? で識別し YAML 定義と照合できる"""
    session = await session_mgr.identify(PMX_RESOURCE)

    assert "KIKUSUI" in session.idn_response.upper()
    assert "PMX35-3A" in session.idn_response
    assert session.definition is not None, f"定義なし (IDN: {session.idn_response!r})"
    assert session.definition.metadata.manufacturer == "Kikusui"
    assert session.definition.metadata.model == "PMX35-3A"


# ---------------------------------------------------------------------------
# 3. PMX35-3A: 読み取り専用クエリ群
# ---------------------------------------------------------------------------

async def test_pmx_query_voltage(session_mgr):
    """設定電圧値を問い合わせる (VOLT?)"""
    response = await session_mgr._visa.query(PMX_RESOURCE, "VOLT?", timeout_ms=3000)
    value = float(response.strip())
    assert 0.0 <= value <= 36.75, f"電圧値が範囲外: {value}"


async def test_pmx_query_current(session_mgr):
    """設定電流値を問い合わせる (CURR?)"""
    response = await session_mgr._visa.query(PMX_RESOURCE, "CURR?", timeout_ms=3000)
    value = float(response.strip())
    assert 0.0 <= value <= 3.15, f"電流値が範囲外: {value}"


async def test_pmx_query_output_state(session_mgr):
    """出力状態を問い合わせる (OUTP?)"""
    response = await session_mgr._visa.query(PMX_RESOURCE, "OUTP?", timeout_ms=3000)
    assert response.strip() in ("0", "1"), f"不明な出力状態: {response!r}"


async def test_pmx_query_error(session_mgr):
    """エラーキューを読み出す (SYST:ERR?)"""
    response = await session_mgr._visa.query(PMX_RESOURCE, "SYST:ERR?", timeout_ms=3000)
    # 正常時は +0,"No error" 系
    assert response.strip() != "", "応答が空"


async def test_pmx_query_ovp(session_mgr):
    """OVP 設定値を問い合わせる (VOLT:PROT?)"""
    response = await session_mgr._visa.query(PMX_RESOURCE, "VOLT:PROT?", timeout_ms=3000)
    value = float(response.strip())
    assert 3.5 <= value <= 38.5, f"OVP が範囲外: {value}"


async def test_pmx_query_ocp(session_mgr):
    """OCP 設定値を問い合わせる (CURR:PROT?)"""
    response = await session_mgr._visa.query(PMX_RESOURCE, "CURR:PROT?", timeout_ms=3000)
    value = float(response.strip())
    assert 0.3 <= value <= 3.3, f"OCP が範囲外: {value}"


# ---------------------------------------------------------------------------
# 4. PMX35-3A: 安全制約 — precondition ブロック
# ---------------------------------------------------------------------------

def test_pmx_output_on_blocked_without_protection(session_mgr):
    """
    OVP/OCP を設定せずに output ON を試みると precondition 違反が返る。
    実際にコマンドは送信しない（sf.validate のみ）。
    """
    from visa_mcp import safety as sf

    session = session_mgr.get_session(PMX_RESOURCE)
    assert session is not None, "PMX セッションが未確立"

    # command_history が空の状態で set_output ON を検証
    session.command_history.clear()
    violations = sf.validate(
        session.definition,
        "set_output",
        {"state": "ON"},
        session_history=[],
    )

    assert len(violations) > 0, "precondition 違反が検出されるべき"
    # 違反メッセージ (SafetyViolation は dict 派生) に OVP/OCP 関連の記述があること
    messages = " ".join(v.get("details", "") for v in violations)
    assert any(kw in messages for kw in ["保護", "OVP", "OCP", "protection"]), \
        f"予期しない違反メッセージ: {messages}"


def test_pmx_output_on_allowed_after_protection(session_mgr):
    """
    OVP/OCP 設定済み履歴があれば output ON の precondition が通る。
    """
    from visa_mcp import safety as sf

    session = session_mgr.get_session(PMX_RESOURCE)
    assert session is not None

    violations = sf.validate(
        session.definition,
        "set_output",
        {"state": "ON"},
        session_history=["set_voltage_protection", "set_current_protection"],
    )

    assert len(violations) == 0, f"不要な violations: {[v.message for v in violations]}"


# ---------------------------------------------------------------------------
# 5. v0.4.1 — SCPI ロングフォーム危険キーワード検出（実機送信なし）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cmd,expected_label", [
    ("VOLTAGE 5",           "VOLT"),
    ("CURRENT 1",           "CURR"),
    ("OUTPUT ON",           "OUTP"),
    ("SOURCE:VOLTAGE 10",   "VOLT"),
    ("CONFIGURE:VOLTAGE",   "CONF"),
    ("RANGE 10",            "RANG"),
    ("INITIATE",            "INIT"),
    ("TRIGGER",             "TRIG"),
])
def test_longform_detection(cmd, expected_label):
    """v0.4.1: SCPI ロングフォームが危険キーワードとして検出される"""
    from lab_executor.tools.commands import _detect_dangerous_keywords
    hits = _detect_dangerous_keywords(cmd)
    assert expected_label in hits, f"'{cmd}' から '{expected_label}' 未検出。実際: {hits}"


@pytest.mark.parametrize("cmd", [
    "CONF:VOLT;READ?",
    "INIT;*OPC?",
    "VOLTAGE 5;*OPC?",
])
def test_compound_query_detection(cmd):
    """v0.4.1: ; を含む複合コマンドは ? があっても危険と判定される"""
    from lab_executor.tools.commands import _detect_dangerous_keywords
    assert _detect_dangerous_keywords(cmd) != [], f"'{cmd}' は危険と判定されるべき"


@pytest.mark.parametrize("cmd", [
    "*IDN?", "VOLT?", "CURR?", "OUTP?", "SYST:ERR?",
])
def test_pure_query_safe(cmd):
    """v0.4.1: pure query (? のみ) は安全扱い"""
    from lab_executor.tools.commands import _detect_dangerous_keywords
    assert _detect_dangerous_keywords(cmd) == [], f"'{cmd}' は安全のはず"


# ---------------------------------------------------------------------------
# 6. Yokogawa 7563: 手動バインドと測定読み取り
# ---------------------------------------------------------------------------

def test_yokogawa_bind(session_mgr):
    """Yokogawa 7563 を手動バインドできる"""
    session = session_mgr.bind_manually(YOK_RESOURCE, "Yokogawa", "7563")

    assert session is not None, "バインド失敗"
    assert session.definition is not None
    assert session.definition.metadata.model == "7563"
    assert "read_measurement" in session.definition.commands


async def test_yokogawa_read_measurement(session_mgr):
    """
    GPIB トーカ機能で測定データを読み取る。
    Yokogawa 7563 は *IDN? 非対応のため、空クエリで読み出す。
    """
    # 空 SCPI = GPIB リード (read_measurement コマンドと同等)
    response = await session_mgr._visa.query(YOK_RESOURCE, "", timeout_ms=5000)
    assert response.strip() != "", "測定データが空"

    # 応答フォーマット解析
    from lab_executor.response_parser import parse_with_definition
    session = session_mgr.get_session(YOK_RESOURCE)
    assert session is not None

    parsed = parse_with_definition(response, session.definition, "measurement_data")
    if parsed:
        # 正常応答なら status="N" (Normal)
        print(f"\n  Yokogawa 7563 測定値: {response.strip()}")
        print(f"  解析結果: {parsed}")
    else:
        # 応答フォーマットが一致しない場合でもエラーにしない（生データを確認）
        print(f"\n  Yokogawa 7563 生データ: {response.strip()!r}")
