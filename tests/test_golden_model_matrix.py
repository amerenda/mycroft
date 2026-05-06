"""Regression tests for workflows/testing/tools/golden_model_matrix.py helpers."""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path


def _load_golden_module():
    root = Path(__file__).resolve().parent.parent
    path = root / "workflows" / "testing" / "tools" / "golden_model_matrix.py"
    spec = importlib.util.spec_from_file_location("golden_model_matrix", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load golden_model_matrix spec")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["golden_model_matrix"] = mod
    spec.loader.exec_module(mod)
    return mod


gm = _load_golden_module()


def test_wf_slug_is_safe_name_and_bounded():
    """Workflow names must satisfy coordinator _safe_name: lowercase [a-z0-9_-]{0,63}."""
    slug = gm.wf_slug("Qwen/Qwen2.5:32b", "8261cadd26")
    assert re.match(r"^[a-z0-9][a-z0-9_-]{0,63}$", slug)
    assert len(slug) <= 64
    assert slug.startswith("rn-g8261ca")


def test_wf_slug_per_agent_is_safe_name_and_bounded():
    slug = gm.wf_slug_per_agent("a/b:1", "c:d", "e-f:3", "8261cadd26")
    assert re.match(r"^[a-z0-9][a-z0-9_-]{0,63}$", slug)
    assert len(slug) <= 64
    assert slug.startswith("rn-pa8261ca")


def test_select_new_models_excludes_baseline_and_prefers_fits():
    models = [
        {"name": "ministral-3:14b", "fits": True, "is_alias": False},
        {"name": "qwen3.5:9b", "fits": True, "is_alias": False},
        {"name": "qwen3:30b", "fits": True, "is_alias": False, "parameter_count": 30},
        {"name": "llama3.1:8b", "fits": True, "is_alias": False, "parameter_count": 8},
        {"name": "gemma2:9b", "fits": True, "is_alias": False, "parameter_count": 9},
        {"name": "big-unfit", "fits": False, "is_alias": False, "parameter_count": 70},
    ]
    picked = gm.select_new_models(models, max_models=4)
    assert "ministral-3:14b" not in picked
    assert "qwen3.5:9b" not in picked
    assert "big-unfit" not in picked
    assert set(picked) <= {"qwen3:30b", "llama3.1:8b", "gemma2:9b"}


def test_select_new_models_falls_back_when_nothing_fits():
    """If every row is fits=false, still return non-baseline names (matrix can probe queue)."""
    models = [
        {"name": "ministral-3:14b", "fits": False},
        {"name": "phi4:latest", "fits": False, "is_alias": False},
    ]
    picked = gm.select_new_models(models, max_models=2)
    assert picked == ["phi4:latest"]


def test_build_pipeline_sets_model_everywhere_and_researcher_prompt_override():
    base = {
        "pipeline_json": {
            "description": "seed",
            "steps": [
                {"agent": "web-search", "model": "old-a"},
                {"agent": "researcher", "model": "old-b"},
                {"agent": "report-writer", "model": "old-c"},
            ],
        }
    }
    pj = gm.build_pipeline(base, "llama3.1:8b", "8261cadd26")
    steps = pj["steps"]
    assert len(steps) == 3
    for s in steps:
        assert s["model"] == "llama3.1:8b"
    assert "prompt_override" not in steps[0]
    assert "prompt_override" in steps[1]
    assert gm.GOLDEN_PROMPTS["8261cadd26"] in steps[1]["prompt_override"]
    assert "prompt_override" not in steps[2]
    assert "golden_matrix model=llama3.1:8b prompt=8261cadd26" in pj["description"]


def test_build_pipeline_pins_report_writer_when_requested():
    base = {
        "pipeline_json": {
            "description": "seed",
            "steps": [
                {"agent": "web-search"},
                {"agent": "researcher"},
                {"agent": "report-writer"},
            ],
        }
    }
    pj = gm.build_pipeline(
        base, "qwen2.5-coder:7b", "8261cadd26", report_writer_model="mistral-small3.2:24b"
    )
    st = pj["steps"]
    assert st[0]["model"] == "qwen2.5-coder:7b"
    assert st[1]["model"] == "qwen2.5-coder:7b" and "prompt_override" in st[1]
    assert st[2]["model"] == "mistral-small3.2:24b" and "prompt_override" not in st[2]
    assert "report_writer=mistral-small3.2:24b" in pj["description"]


def test_golden_prompt_catalog_matches_research_findings():
    """Top-scoring QUIC variants from 2026-05-01 sweep must remain addressable."""
    assert "8261cadd26" in gm.GOLDEN_PROMPTS
    assert "5bc1060d34" in gm.GOLDEN_PROMPTS
    assert gm.DEFAULT_PROMPT_ORDER[0] == "8261cadd26"
    assert "QUIC" in gm.COMMON_QUERY and "HTTP/2" in gm.COMMON_QUERY


def test_build_pipeline_per_agent_assigns_distinct_models():
    base = {
        "pipeline_json": {
            "description": "x",
            "steps": [
                {"agent": "web-search"},
                {"agent": "researcher"},
                {"agent": "report-writer"},
            ],
        }
    }
    pj = gm.build_pipeline_per_agent(
        base,
        web_search="qwen3.5:9b",
        researcher="ministral-3:14b",
        report_writer="llama3.1:8b",
        prompt_hash="8261cadd26",
    )
    st = pj["steps"]
    assert st[0]["model"] == "qwen3.5:9b" and "prompt_override" not in st[0]
    assert st[1]["model"] == "ministral-3:14b" and "prompt_override" in st[1]
    assert st[2]["model"] == "llama3.1:8b" and "prompt_override" not in st[2]
