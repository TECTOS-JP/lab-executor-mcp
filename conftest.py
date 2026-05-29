"""Project-level conftest: ensure local `src/` on sys.path so tests
run without `pip install -e .` (useful in network-restricted envs)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if SRC.exists() and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
