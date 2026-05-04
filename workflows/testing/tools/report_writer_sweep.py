#!/usr/bin/env python3
"""
Run the golden matrix once per report-writer candidate (same matrix model + same instruction),
score summary_preview with a cheap heuristic, then re-run a larger matrix with the best writer.

Environment (optional):
  SWEEP_MATRIX_MODEL       Model for web-search + researcher (default: qwen3.5:9b)
  SWEEP_REPORT_WRITERS     Comma-separated report-writer models to try
  MYCROFT_URL, LLM_MANAGER_URL, MYCROFT_API_KEY, GOLDEN_MATRIX_TIMEOUT — same as golden_model_matrix.py

Example:
  export MYCROFT_URL=https://mycroft.amer.dev LLM_MANAGER_URL=https://llm-manager.amer.dev
  python workflows/testing/tools/report_writer_sweep.py
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path


def score_report_preview(text: str | None) -> tuple[int, str]:
    """Higher is better. Negative = clearly bad."""
    t = (text or "").strip()
    if len(t) < 120:
        return -100, "too_short"
    head = t[:1200].lower()
    if "```json" in head or re.match(r"^```\s*json\s*$", t[:40].lower().replace("\n", " "), re.I):
        return -80, "json_fence"
    if '"name"' in head and '"arguments"' in head and "{" in t[:400]:
        return -60, "tool_call_shape"
    score = 0
    if "##" in t or "###" in t:
        score += 25
    if "https://" in t or "http://" in t:
        score += 18
    if re.search(r"\n\n[^\n]", t):
        score += 8
    score += min(len(t) // 400, 12)
    return score, "ok"


def _slug(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "_", name).strip("_")[:48]


def main() -> int:
    tools = Path(__file__).resolve().parent
    repo_root = tools.parent.parent.parent
    golden = tools / "golden_model_matrix.py"
    matrix_model = os.environ.get("SWEEP_MATRIX_MODEL", "qwen3.5:9b").strip()
    writers_raw = os.environ.get(
        "SWEEP_REPORT_WRITERS",
        "ministral-3:14b,llama3.1:8b,mistral-small3.2:24b,qwen3.5:9b",
    )
    writers = [w.strip() for w in writers_raw.split(",") if w.strip()]
    if not writers:
        print("No SWEEP_REPORT_WRITERS", file=sys.stderr)
        return 1

    today = str(date.today())
    print(f"Sweep matrix_model={matrix_model!r} writers={writers}", flush=True)

    results: list[dict[str, object]] = []
    for rw in writers:
        wd = f"rw-sweep-{_slug(rw)}"
        out_dir = tools.parent / wd / today
        cmd = [
            sys.executable,
            str(golden),
            "--matrix-model",
            matrix_model,
            "--max-models",
            "1",
            "--max-prompts",
            "1",
            "--max-running",
            "1",
            "--report-writer-model",
            rw,
            "--workflow-dir",
            wd,
            "--stagger-sec",
            "3",
        ]
        print(f"\n--- RUN writer={rw!r} workflow-dir={wd} ---", flush=True)
        rc = subprocess.run(cmd, cwd=str(repo_root))
        if rc.returncode != 0:
            results.append({"report_writer": rw, "rc": rc.returncode, "score": -999, "reason": "run_failed"})
            continue
        raw_path = out_dir / "golden-model-matrix-raw.json"
        if not raw_path.is_file():
            results.append({"report_writer": rw, "rc": rc.returncode, "score": -998, "reason": "no_raw_json"})
            continue
        data = json.loads(raw_path.read_text())
        runs = data.get("runs") or []
        prev = ""
        if runs and isinstance(runs[0], dict):
            prev = str((runs[0].get("summary_preview") or ""))
        sc, tag = score_report_preview(prev)
        results.append(
            {
                "report_writer": rw,
                "rc": 0,
                "score": sc,
                "reason": tag,
                "preview_head": prev[:180].replace("\n", " "),
            }
        )
        print(f"score={sc} ({tag}) preview={results[-1]['preview_head']!r}", flush=True)

    best = max(results, key=lambda r: int(r.get("score", -9999)))
    print("\n=== SWEEP SUMMARY ===", flush=True)
    for r in sorted(results, key=lambda x: int(x.get("score", -9999)), reverse=True):
        print(f"  {r.get('report_writer')}: score={r.get('score')} {r.get('reason')} rc={r.get('rc')}", flush=True)
    winner = str(best.get("report_writer") or "")
    wscore = int(best.get("score", -9999))
    if not winner or wscore < 0:
        print("No acceptable report-writer winner (all scores negative or run failures).", file=sys.stderr)
        return 2

    print(f"\nWinner: {winner!r} (score={wscore}) — running confirmation matrix…", flush=True)
    wd2 = f"rw-confirm-{_slug(winner)}"
    cmd2 = [
        sys.executable,
        str(golden),
        "--matrix-model",
        matrix_model,
        "--max-models",
        "1",
        "--max-prompts",
        "4",
        "--max-running",
        "2",
        "--report-writer-model",
        winner,
        "--workflow-dir",
        wd2,
        "--stagger-sec",
        "3",
    ]
    rc2 = subprocess.run(cmd2, cwd=str(repo_root))
    ex = rc2.returncode if rc2.returncode is not None else 1
    print(f"Confirmation run exit={ex} artifacts under workflows/testing/{wd2}/{today}/", flush=True)
    return int(ex)


if __name__ == "__main__":
    raise SystemExit(main())
