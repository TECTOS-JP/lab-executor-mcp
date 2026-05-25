"""examples/instruments/ に同梱した YAML がスキーマ的に有効であることを確認するテスト。"""
from pathlib import Path
from lab_executor.instrument_registry import InstrumentRegistry

EXAMPLES_DIR = Path(__file__).parent.parent / "examples" / "instruments"


def test_examples_instruments_load():
    """examples/instruments/ の全 YAML が問題なくロードできる。"""
    if not EXAMPLES_DIR.exists():
        return  # 同梱サンプルがない場合はスキップ
    reg = InstrumentRegistry(EXAMPLES_DIR)
    defs = reg.list_definitions()
    # サンプルが空でないこと（同梱されていれば）
    yamls = [
        f for f in EXAMPLES_DIR.glob("*.yaml") if not f.name.startswith("_")
    ]
    assert len(defs) == len(yamls), (
        f"YAML ファイル {len(yamls)} 個に対しロードできたのは {len(defs)} 個。"
        " スキーマ違反の可能性。"
    )


def test_examples_each_has_commands():
    """各サンプルが少なくとも 1 つのコマンドを持つ。"""
    if not EXAMPLES_DIR.exists():
        return
    reg = InstrumentRegistry(EXAMPLES_DIR)
    for d in reg.list_definitions():
        assert d["command_count"] >= 1, f"{d['manufacturer']} {d['model']} にコマンドがない"
