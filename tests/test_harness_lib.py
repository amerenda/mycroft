"""Unit tests for workflows/testing/tools/harness_lib.py (no live services)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_harness():
    root = Path(__file__).resolve().parent.parent
    path = root / "workflows" / "testing" / "tools" / "harness_lib.py"
    spec = importlib.util.spec_from_file_location("harness_lib", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load harness_lib spec")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["harness_lib"] = mod
    spec.loader.exec_module(mod)
    return mod


hl = _load_harness()


def test_wf_slug_prefix_matches_golden_matrix_convention():
    slug = hl.wf_slug_prefix("rn-g", "Qwen/Qwen2.5:32b", "8261cadd26")
    assert slug.startswith("rn-g8261ca")
    assert len(slug) <= 64


def test_select_new_models_excludes_baseline():
    models = [
        {"name": "ministral-3:14b", "fits": True, "is_alias": False},
        {"name": "qwen3.5:9b", "fits": True, "is_alias": False},
        {"name": "llama3.1:8b", "fits": True, "is_alias": False},
    ]
    picked = hl.select_new_models(
        models, max_models=2, baseline_models=frozenset({"ministral-3:14b", "qwen3.5:9b"})
    )
    assert picked == ["llama3.1:8b"]


def test_testing_output_dir_normpath():
    d = hl.testing_output_dir("/abs/tools", "my-workflow", "2026-05-01")
    assert "my-workflow" in d
    assert d.endswith("2026-05-01")


def test_param_sort_key_numeric():
    assert hl.param_sort_key({"parameter_count": 14, "name": "a"}) == 14.0
    assert hl.param_sort_key({"parameter_count": "9B", "name": "b"}) == 9.0
