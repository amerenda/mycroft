#!/usr/bin/env python3
"""
Scan llm-manager GET /api/models (no auth) and run research-new E2E variants
with golden researcher prompt_override + one model across all pipeline steps.

Environment:
  LLM_MANAGER_URL   Base URL for llm-manager (default: http://127.0.0.1:8081)
  MYCROFT_URL       Base URL for mycroft coordinator (default: http://127.0.0.1:8080)
  MYCROFT_API_KEY   Optional Bearer token for coordinator

Usage:
  LLM_MANAGER_URL=https://llm.example.com MYCROFT_URL=http://127.0.0.1:18088 \\
    python workflows/testing/tools/golden_model_matrix.py

Options:
  --workflow-dir    Subfolder under workflows/testing/ for outputs (default: research-new)
  --source-workflow Coordinator workflow to clone (default: research-new)
  --terminal-agent  Agent type to wait for (default: report-writer)
  --dry-scan        Only fetch models, print selection, exit
  --max-models N    Cap distinct new models (default 4)
  --max-prompts M   Cap golden prompts (default 4)
  --stagger-sec S   Sleep between POST /api/tasks (default 25)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

_TOOLS_DIR = str(Path(__file__).resolve().parent)
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)
import harness_lib as hl  # noqa: E402

LLM_MANAGER_URL = os.environ.get("LLM_MANAGER_URL", "http://127.0.0.1:8081").rstrip("/")
MYCROFT_URL = os.environ.get("MYCROFT_URL", "http://127.0.0.1:8080").rstrip("/")
MYCROFT_API_KEY = os.environ.get("MYCROFT_API_KEY", "").strip()

# Models heavily used in the 2026-05-01 QUIC sweep — skip when picking "new"
BASELINE_MODELS = frozenset({"ministral-3:14b", "qwen3.5:9b"})

# Full golden set from workflows/testing/research-new/2026-05-01/prompts.md
GOLDEN_PROMPTS: dict[str, str] = {
    "fdd9ef09fd": (
        "You are a technical analyst. Answer directly with only source-grounded claims. "
        "Require at least 5 citations from provided source URLs. If evidence is weak, say unknown."
    ),
    "3132728a72": (
        "Produce a concise report with sections: Direct answer, Evidence, Caveats, Sources. "
        "Do not invent commands, metrics, or adoption percentages. Every claim ends with URL citation."
    ),
    "8261cadd26": (
        "Prioritize primary sources (RFCs, vendor/browser docs). Treat blogs/forums/wiki as secondary. "
        "Explicitly label confidence High/Medium/Low for each key claim."
    ),
    "5bc1060d34": (
        "Use run_fetch_list and run_fetch_read first to validate evidence. If source pack conflicts "
        "with fetched text, trust fetched text and mention contradiction."
    ),
    "84612fc2d0": (
        "Return only markdown. No JSON. No decorative text. Include exactly: Direct answer, "
        "On-the-wire differences, Browser QUIC usage conditions, Fallback behavior, Sources."
    ),
    "adc2debabf": (
        "If question is networking protocol behavior, avoid hardware/model-serving templates. "
        "Focus on protocol mechanics and browser negotiation behavior with citations."
    ),
    "c6eb668585": (
        "For each key statement add one quote snippet in parentheses from evidence plus URL. "
        "Keep report under 400 words."
    ),
    "d87fa6d4bd": (
        "Explain what triggers QUIC use in browsers (ALPN, Alt-Svc, UDP availability, fallback). "
        "If uncertain, state uncertainty instead of guessing."
    ),
    "d7d5d34d8f": (
        "Strict anti-hallucination mode: do not output any claim not present in provided evidence "
        "or follow-up reads. Prefer omission to speculation."
    ),
    "53952735f5": (
        "Write an implementation-ready troubleshooting view: detection signals that QUIC is in use, "
        "common blockers, and fallback paths; cite sources for each bullet."
    ),
}

# Highest-scoring hashes first (from results.md)
DEFAULT_PROMPT_ORDER = [
    "8261cadd26",
    "fdd9ef09fd",
    "84612fc2d0",
    "5bc1060d34",
    "d7d5d34d8f",
    "3132728a72",
    "d87fa6d4bd",
    "53952735f5",
    "adc2debabf",
    "c6eb668585",
]

COMMON_QUERY = (
    "What is the difference between QUIC and HTTP/2 on the wire, and when does a browser "
    "actually use QUIC?"
)

SOURCE_WORKFLOW = "research-new"
PER_RUN_TIMEOUT = int(os.environ.get("GOLDEN_MATRIX_TIMEOUT", "7200"))
RUN_ID_WAIT = 180


def select_new_models(models: list[dict[str, Any]], max_models: int) -> list[str]:
    return hl.select_new_models(models, max_models, baseline_models=BASELINE_MODELS)


def wf_slug(model: str, h: str) -> str:
    return hl.wf_slug_prefix("rn-g", model, h)


def build_pipeline(base_wf: dict[str, Any], model: str, prompt_hash: str) -> dict[str, Any]:
    pj = json.loads(json.dumps(base_wf.get("pipeline_json") or {}))
    steps = pj.get("steps") or []
    suffix = GOLDEN_PROMPTS[prompt_hash]
    new_steps: list[dict[str, Any]] = []
    for s in steps:
        s2 = dict(s)
        s2["model"] = model
        if (s2.get("agent") or "") == "researcher":
            s2["prompt_override"] = suffix
        new_steps.append(s2)
    pj["steps"] = new_steps
    desc = (pj.get("description") or "").strip()
    tag = f" golden_matrix model={model} prompt={prompt_hash}"
    pj["description"] = (desc + tag)[:2000]
    return pj


def build_pipeline_per_agent(
    base_wf: dict[str, Any],
    *,
    web_search: str,
    researcher: str,
    report_writer: str,
    prompt_hash: str,
    description_tag: str = "",
) -> dict[str, Any]:
    """Set a distinct model per pipeline step; researcher gets golden prompt_override."""
    pj = json.loads(json.dumps(base_wf.get("pipeline_json") or {}))
    steps = pj.get("steps") or []
    by_agent = {
        "web-search": web_search,
        "researcher": researcher,
        "report-writer": report_writer,
    }
    suffix = GOLDEN_PROMPTS[prompt_hash]
    new_steps: list[dict[str, Any]] = []
    for s in steps:
        s2 = dict(s)
        agent = (s2.get("agent") or "").strip()
        if agent in by_agent:
            s2["model"] = by_agent[agent]
        if agent == "researcher":
            s2["prompt_override"] = suffix
        new_steps.append(s2)
    pj["steps"] = new_steps
    desc = (pj.get("description") or "").strip()
    tag = (
        f" per_agent_matrix web={web_search} researcher={researcher} "
        f"report={report_writer} prompt={prompt_hash}"
    )
    if description_tag:
        tag = f"{tag} {description_tag}"
    pj["description"] = (desc + tag)[:2000]
    return pj


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workflow-dir", default="research-new", help="Subfolder under workflows/testing/")
    ap.add_argument("--source-workflow", default=SOURCE_WORKFLOW, help="Base workflow name in coordinator")
    ap.add_argument(
        "--terminal-agent",
        default="report-writer",
        help="Poll until this agent_type is completed or failed",
    )
    ap.add_argument("--dry-scan", action="store_true", help="Only list models from llm-manager")
    ap.add_argument("--max-models", type=int, default=4)
    ap.add_argument("--max-prompts", type=int, default=4)
    ap.add_argument("--stagger-sec", type=float, default=25.0)
    args = ap.parse_args()

    today = str(date.today())
    tools_dir = str(Path(__file__).resolve().parent)
    out_dir = hl.testing_output_dir(tools_dir, args.workflow_dir, today)
    os.makedirs(out_dir, exist_ok=True)

    print(f"LLM_MANAGER_URL={LLM_MANAGER_URL}", flush=True)
    try:
        models = hl.fetch_models(LLM_MANAGER_URL)
    except (urllib.error.URLError, TimeoutError, RuntimeError) as e:
        print(f"ERROR: cannot reach llm-manager /api/models: {e}", file=sys.stderr)
        return 1

    scan_path = os.path.join(out_dir, "model-scan.json")
    with open(scan_path, "w") as f:
        json.dump(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "source": LLM_MANAGER_URL + "/api/models",
                "count": len(models),
                "models": models,
            },
            f,
            indent=2,
        )
    print(f"Wrote {scan_path} ({len(models)} entries)", flush=True)

    chosen = select_new_models(models, args.max_models)
    print("Selected new models:", chosen or "(none — relax filters or add pulls)", flush=True)

    if args.dry_scan:
        return 0

    if not chosen:
        print("No models to run; exiting.", file=sys.stderr)
        return 1

    prompt_hashes = [h for h in DEFAULT_PROMPT_ORDER if h in GOLDEN_PROMPTS][: args.max_prompts]
    print(f"MYCROFT_URL={MYCROFT_URL}", flush=True)
    print("Prompt hashes:", prompt_hashes, flush=True)

    try:
        base_wf = hl.http_json(
            "GET",
            MYCROFT_URL,
            f"/api/workflows/{args.source_workflow}",
            None,
            timeout=60.0,
            bearer_token=MYCROFT_API_KEY,
        )
    except (urllib.error.URLError, TimeoutError) as e:
        print(f"ERROR: cannot reach mycroft coordinator: {e}", file=sys.stderr)
        print("Model scan is saved; start port-forward / fix URL and re-run without --dry-scan.", file=sys.stderr)
        return 2

    if not isinstance(base_wf, dict) or "pipeline_json" not in base_wf:
        print("ERROR: unexpected GET workflow response", file=sys.stderr)
        return 1

    runs: list[dict[str, Any]] = []
    for model in chosen:
        for ph in prompt_hashes:
            name = wf_slug(model, ph)
            pj = build_pipeline(base_wf, model, ph)
            try:
                hl.put_workflow(
                    MYCROFT_URL,
                    name,
                    content=base_wf.get("content") or "",
                    pipeline_json=pj,
                    bearer_token=MYCROFT_API_KEY,
                )
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", errors="replace") if e.fp else ""
                print(f"PUT workflow {name} failed: {e.code} {body[:500]}", file=sys.stderr)
                runs.append(
                    {
                        "workflow": name,
                        "model": model,
                        "prompt_hash": ph,
                        "status": "put_failed",
                        "detail": str(e),
                    }
                )
                continue

            time.sleep(2)
            try:
                task = hl.post_task_workflow(
                    MYCROFT_URL, name, COMMON_QUERY, bearer_token=MYCROFT_API_KEY
                )
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", errors="replace") if e.fp else ""
                runs.append(
                    {
                        "workflow": name,
                        "model": model,
                        "prompt_hash": ph,
                        "status": "post_failed",
                        "detail": f"{e.code} {body[:400]}",
                    }
                )
                time.sleep(args.stagger_sec)
                continue

            tid = (task or {}).get("task_id")
            rid = (
                hl.wait_run_id(
                    MYCROFT_URL,
                    tid,
                    bearer_token=MYCROFT_API_KEY,
                    run_id_wait_sec=float(RUN_ID_WAIT),
                )
                if tid
                else None
            )
            rec: dict[str, Any] = {
                "workflow": name,
                "model": model,
                "prompt_hash": ph,
                "query": COMMON_QUERY,
                "first_task_id": tid,
                "run_id": rid,
            }
            if not rid:
                rec["status"] = "no_run_id"
                rec["final_task_id"] = None
            else:
                rw = hl.wait_until_agent_terminal(
                    MYCROFT_URL,
                    name,
                    rid,
                    args.terminal_agent,
                    bearer_token=MYCROFT_API_KEY,
                    per_run_timeout_sec=float(PER_RUN_TIMEOUT),
                )
                if rw is None:
                    rec["status"] = "timeout"
                    rec["final_task_id"] = None
                else:
                    rec["final_task_id"] = rw["id"]
                    rec["status"] = rw["status"]
                    td = hl.http_json(
                        "GET",
                        MYCROFT_URL,
                        f"/api/tasks/{rw['id']}",
                        None,
                        timeout=60.0,
                        bearer_token=MYCROFT_API_KEY,
                    )
                    res = (td.get("result") or {}) if isinstance(td, dict) else {}
                    rec["summary_preview"] = (res.get("summary") or "")[:600]
                    rec["error"] = res.get("error")
            runs.append(rec)
            print(f"{name} -> {rec.get('status')}", flush=True)
            time.sleep(args.stagger_sec)

    raw_path = os.path.join(out_dir, "golden-model-matrix-raw.json")
    with open(raw_path, "w") as f:
        json.dump(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "common_query": COMMON_QUERY,
                "baseline_excluded": sorted(BASELINE_MODELS),
                "source_workflow": args.source_workflow,
                "terminal_agent": args.terminal_agent,
                "runs": runs,
            },
            f,
            indent=2,
        )
    print(f"Wrote {raw_path}", flush=True)

    agent_col = args.terminal_agent.replace("-", " ")
    lines = [
        f"# Golden prompt × new model matrix ({today})",
        "",
        f"Common query: *{COMMON_QUERY}*",
        "",
        f"| workflow | model | prompt hash | status | {agent_col} task |",
        "|---|---|---|---|---|",
    ]
    for r in runs:
        lines.append(
            "| {wf} | `{m}` | `{h}` | {st} | {ft} |".format(
                wf=r.get("workflow", "-"),
                m=r.get("model", "-"),
                h=r.get("prompt_hash", "-"),
                st=r.get("status", "-"),
                ft=r.get("final_task_id") or "-",
            )
        )
    lines.append("")
    lines.append(f"Scan: `{scan_path}`")
    lines.append(f"Raw: `{raw_path}`")
    lines.append("")
    lines.append("Score each completed run 0–10 in a follow-up edit (see workflows/testing/README.md).")
    md_path = os.path.join(out_dir, "golden-model-matrix-results.md")
    with open(md_path, "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"Wrote {md_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
