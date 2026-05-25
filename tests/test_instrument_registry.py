"""InstrumentRegistry の統合テスト: YAML ファイルをロードし、定義検索が動作する。"""
import textwrap
from lab_executor.instrument_registry import InstrumentRegistry


def write_yaml(tmp_path, name, content):
    p = tmp_path / name
    p.write_text(textwrap.dedent(content), encoding="utf-8")


def test_load_multiple_definitions(tmp_path):
    write_yaml(tmp_path, "vendor_a.yaml", """
        metadata:
          manufacturer: "VendorA"
          model: "Model1"
          description: "test1"
        commands:
          identify:
            scpi: "*IDN?"
            type: "query"
            description: "id"
    """)
    write_yaml(tmp_path, "vendor_b.yaml", """
        metadata:
          manufacturer: "VendorB"
          model: "Model2"
          description: "test2"
        commands:
          reset:
            scpi: "*RST"
            type: "write"
            description: "reset"
    """)

    reg = InstrumentRegistry(tmp_path)
    defs = reg.list_definitions()
    assert len(defs) == 2
    names = {(d["manufacturer"], d["model"]) for d in defs}
    assert ("VendorA", "Model1") in names
    assert ("VendorB", "Model2") in names


def test_template_files_are_skipped(tmp_path):
    write_yaml(tmp_path, "_template.yaml", """
        metadata:
          manufacturer: "TEMPLATE"
          model: "TEMPLATE"
          description: "do not load"
        commands: {}
    """)
    write_yaml(tmp_path, "real.yaml", """
        metadata:
          manufacturer: "Real"
          model: "Real1"
          description: "test"
        commands:
          identify:
            scpi: "*IDN?"
            type: "query"
            description: "id"
    """)
    reg = InstrumentRegistry(tmp_path)
    assert len(reg.list_definitions()) == 1


def test_get_definition_case_sensitive_match(tmp_path):
    write_yaml(tmp_path, "x.yaml", """
        metadata:
          manufacturer: "Kikusui"
          model: "PMX35-3A"
          description: "test"
        commands:
          identify:
            scpi: "*IDN?"
            type: "query"
            description: "id"
    """)
    reg = InstrumentRegistry(tmp_path)
    d = reg.get_definition("Kikusui", "PMX35-3A")
    assert d is not None
    assert d.metadata.manufacturer == "Kikusui"


def test_get_definition_returns_none_when_missing(tmp_path):
    reg = InstrumentRegistry(tmp_path)
    assert reg.get_definition("Nope", "Nothing") is None


def test_invalid_yaml_is_skipped(tmp_path, caplog):
    write_yaml(tmp_path, "broken.yaml", "this is: not: valid: yaml: :::")
    write_yaml(tmp_path, "good.yaml", """
        metadata:
          manufacturer: "OK"
          model: "Fine"
          description: "test"
        commands:
          identify:
            scpi: "*IDN?"
            type: "query"
            description: "id"
    """)
    reg = InstrumentRegistry(tmp_path)
    # 不正な YAML はスキップ、正常な分だけロード
    assert len(reg.list_definitions()) == 1
