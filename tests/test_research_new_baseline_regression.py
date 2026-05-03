"""Regression against workflows/testing/research-new/2026-05-01/results.md (QUIC sweep)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# Parsed from results.md — manual scores + terminal status from that run date.
# If you re-score in the markdown file, update this dict so tests stay aligned.
BASELINE_SCORES: dict[tuple[str, str], int] = {
    ("ministral-3:14b", "fdd9ef09fd"): 8,
    ("qwen3.5:9b", "3132728a72"): 0,
    ("ministral-3:14b", "8261cadd26"): 9,
    ("qwen3.5:9b", "5bc1060d34"): 8,
    ("ministral-3:14b", "84612fc2d0"): 8,
    ("qwen3.5:9b", "adc2debabf"): 0,
    ("ministral-3:14b", "c6eb668585"): 0,
    ("qwen3.5:9b", "d87fa6d4bd"): 7,
    ("ministral-3:14b", "d7d5d34d8f"): 7,
    ("qwen3.5:9b", "53952735f5"): 7,
}

BASELINE_COMPLETED: set[tuple[str, str]] = {
    ("ministral-3:14b", "fdd9ef09fd"),
    ("ministral-3:14b", "8261cadd26"),
    ("qwen3.5:9b", "5bc1060d34"),
    ("ministral-3:14b", "84612fc2d0"),
    ("qwen3.5:9b", "d87fa6d4bd"),
    ("ministral-3:14b", "d7d5d34d8f"),
    ("qwen3.5:9b", "53952735f5"),
}

BASELINE_FAILED_OR_TIMEOUT: set[tuple[str, str]] = {
    ("qwen3.5:9b", "3132728a72"),
    ("qwen3.5:9b", "adc2debabf"),
    ("ministral-3:14b", "c6eb668585"),
}

_RESULTS_MD = (
    Path(__file__).resolve().parent.parent
    / "workflows"
    / "testing"
    / "research-new"
    / "2026-05-01"
    / "results.md"
)


def _scores_from_results_md(text: str) -> dict[tuple[str, str], int]:
    """Parse the score table from results.md (agent | model | prompt hash | score)."""
    out: dict[tuple[str, str], int] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|") or line.startswith("|---"):
            continue
        parts = [p.strip() for p in line.strip("|").split("|")]
        if len(parts) < 4:
            continue
        _agent, model, ph, score_s = parts[0], parts[1], parts[2], parts[3]
        if not re.match(r"^[0-9a-f]{10}$", ph):
            continue
        if model.startswith("`"):
            model = model.strip("`")
        try:
            score = int(score_s)
        except ValueError:
            continue
        out[(model, ph)] = score
    return out


def test_results_md_matches_baseline_scores():
    """Single source of truth: archived results.md must match BASELINE_SCORES."""
    if not _RESULTS_MD.is_file():
        pytest.skip(f"missing {_RESULTS_MD}")
    parsed = _scores_from_results_md(_RESULTS_MD.read_text())
    assert parsed == BASELINE_SCORES


def test_baseline_matrix_covers_ten_model_prompt_pairs():
    assert len(BASELINE_SCORES) == 10
    models = {m for m, _ in BASELINE_SCORES}
    assert models == {"ministral-3:14b", "qwen3.5:9b"}


def test_best_overall_combo_beats_all_others():
    """Single-model pipeline: ministral + 8261cadd26 scored highest (9)."""
    best = max(BASELINE_SCORES.items(), key=lambda kv: kv[1])
    assert best == (("ministral-3:14b", "8261cadd26"), 9)
    assert all(s <= 9 for s in BASELINE_SCORES.values())


def test_best_completed_qwen_combo_is_5bc1060d34():
    """Among qwen3.5:9b rows that completed, 5bc1060d34 is the top score (8)."""
    qwen_done = [(h, s) for (m, h), s in BASELINE_SCORES.items() if m == "qwen3.5:9b" and (m, h) in BASELINE_COMPLETED]
    assert qwen_done
    best_hash, best_s = max(qwen_done, key=lambda x: x[1])
    assert best_hash == "5bc1060d34" and best_s == 8


def test_zero_scores_align_with_infra_or_prompt_failures():
    """Scores of 0 match timeout / failed / incomplete pipeline (per run notes)."""
    zero_keys = {k for k, v in BASELINE_SCORES.items() if v == 0}
    assert zero_keys == BASELINE_FAILED_OR_TIMEOUT


def test_next_five_recommended_runs_are_defined_for_matrix_harness():
    """Five follow-up configs derived from baseline + infra findings (split models, new sizes)."""
    # Rationale briefly in comments; keys consumed by golden_model_matrix / manual runs.
    next_five = [
        {
            "id": 1,
            "web_search": "qwen3.5:9b",
            "researcher": "ministral-3:14b",
            "report_writer": "qwen3.5:9b",
            "researcher_prompt_hash": "8261cadd26",
            "vs_baseline": "Same best prompt (9) but web-search off ministral to reduce queue/Bearer issues.",
        },
        {
            "id": 2,
            "web_search": "qwen3.5:9b",
            "researcher": "ministral-3:14b",
            "report_writer": "llama3.1:8b",
            "researcher_prompt_hash": "fdd9ef09fd",
            "vs_baseline": "Second-best ministral score (8); cheap final pass like report_writer manifest.",
        },
        {
            "id": 3,
            "web_search": "qwen3.5:9b",
            "researcher": "qwen3.5:9b",
            "report_writer": "qwen3.5:9b",
            "researcher_prompt_hash": "5bc1060d34",
            "vs_baseline": "All-qwen repro of best completed qwen row (8) — control when ministral queues.",
        },
        {
            "id": 4,
            "web_search": "qwen3.5:9b",
            "researcher": "ministral-3:14b",
            "report_writer": "qwen3.5:9b",
            "researcher_prompt_hash": "84612fc2d0",
            "vs_baseline": "Structured skeleton prompt also scored 8 on ministral.",
        },
        {
            "id": 5,
            "web_search": "qwen3.5:9b",
            "researcher": "ministral-3:14b",
            "report_writer": "qwen3.5:9b",
            "researcher_prompt_hash": "d7d5d34d8f",
            "vs_baseline": "Strict anti-hallucination (7) — regression guard vs 8261 on harder grounding.",
        },
    ]
    assert len(next_five) == 5
    required = ("web_search", "researcher", "report_writer", "researcher_prompt_hash", "vs_baseline")
    for row in next_five:
        for k in required:
            assert k in row and row[k]
        ph = row["researcher_prompt_hash"]
        assert any(k[1] == ph for k in BASELINE_SCORES)
