"""Shared test fixtures."""

import sys
from pathlib import Path

# Ensure the repo root is on the path so `common`, `runtime`, etc. are importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
