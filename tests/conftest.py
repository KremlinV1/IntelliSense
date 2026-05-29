"""Pytest configuration — makes the in-tree packages importable without
installation. CI should `pip install -e` each package instead; this is a
convenience for `pytest` from a fresh clone."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "shared" / "veilwatch_contracts" / "src"))
sys.path.insert(0, str(ROOT / "services" / "confirmation" / "src"))
sys.path.insert(0, str(ROOT / "services" / "detection" / "src"))
sys.path.insert(0, str(ROOT / "services" / "capture" / "src"))
sys.path.insert(0, str(ROOT / "services" / "tracker" / "src"))
