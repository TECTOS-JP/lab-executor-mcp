"""lab-executor-mcp: backend-independent experiment execution
runtime for AI agents.

v2.0.0: first stable release. visa-mcp v1.11.1 から runtime / DSL /
ecosystem layer を切り出した分離 release。

含まれるもの: DSL / Job / Group / Observation / Benchmark / Definition
pack ecosystem / Instrument authoring / Export / Audit /
InstrumentBackend Protocol + MockBackend。

含まれないもの (visa-mcp 側に残る): PyVisaBackend / VisaManager / raw
VISA tools / hardware resource discovery。

詳細: docs/v2_migration.md / README.md
"""

__version__ = "2.6.0"
