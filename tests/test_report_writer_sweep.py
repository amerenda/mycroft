"""Heuristic tests for workflows/testing/tools/report_writer_sweep.py."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load():
    root = Path(__file__).resolve().parent.parent
    path = root / "workflows" / "testing" / "tools" / "report_writer_sweep.py"
    spec = importlib.util.spec_from_file_location("report_writer_sweep", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("report_writer_sweep")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["report_writer_sweep"] = mod
    spec.loader.exec_module(mod)
    return mod


m = _load()


def test_score_prefers_markdown_report_over_json_fence():
    good = "## Summary\n\nSee [NVIDIA](https://docs.nvidia.com/) for FP4.\n\nDetails follow.\n" * 3
    bad = '```json\n{\n  "name": "query",\n  "arguments": {}\n}\n```'
    sg, _ = m.score_report_preview(good)
    sb, _ = m.score_report_preview(bad)
    assert sg > sb
    assert sb < 0


def test_score_penalizes_short_output():
    s, tag = m.score_report_preview("short")
    assert s < 0 and tag == "too_short"
