#!/usr/bin/env python3
"""
Scan llm-manager GET /api/models (no auth) and run research-new E2E variants
with golden researcher prompt_override + one model across all pipeline steps.

Environment:
  LLM_MANAGER_URL   Base URL for llm-manager (default: http://127.0.0.1:8081)
  MYCROFT_URL       Base URL for mycroft coordinator (default: http://127.0.0.1:8080)
  MYCROFT_API_KEY   Optional Bearer token for coordinator
  GOLDEN_MATRIX_REPORT_WRITER_MODEL  Optional: pin report-writer step to this model while matrix varies researcher (same as --report-writer-model).

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
  --stagger-sec S   After each full run, sleep S seconds (default 25). Only when --max-running=1.
  --max-running N   Concurrent pipelines (async). Default 1 = sequential. Use >1 to speed up or load-test;
                    stagger-sec is then ignored (no sleep between runs).
  --instruction S   Task text for POST /api/tasks (default: built-in QUIC benchmark query).
                    Use a distinct --workflow-dir when changing the instruction on the same calendar day.
  --report-writer-model M   Use model M only on the report-writer step (web-search + researcher still use the matrix model).
                    Avoids coder-tuned matrix models emitting ```json``` tool junk as the final report. Env: GOLDEN_MATRIX_REPORT_WRITER_MODEL.
  --matrix-model M          Pin web-search + researcher to M (skip auto model pick). Use with --max-prompts for prompt sweeps at fixed research capacity.
"""
from __future__ import annotations

import argparse
import asyncio
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


def build_pipeline(
    base_wf: dict[str, Any],
    model: str,
    prompt_hash: str,
    *,
    report_writer_model: str | None = None,
) -> dict[str, Any]:
    pj = json.loads(json.dumps(base_wf.get("pipeline_json") or {}))
    steps = pj.get("steps") or []
    suffix = GOLDEN_PROMPTS[prompt_hash]
    new_steps: list[dict[str, Any]] = []
    rw = (report_writer_model or "").strip() or None
    for s in steps:
        s2 = dict(s)
        agent = (s2.get("agent") or "").strip()
        if agent == "report-writer" and rw:
            s2["model"] = rw
        else:
            s2["model"] = model
        if agent == "researcher":
            s2["prompt_override"] = suffix
        new_steps.append(s2)
    pj["steps"] = new_steps
    desc = (pj.get("description") or "").strip()
    tag = f" golden_matrix model={model} prompt={prompt_hash}"
    if rw:
        tag += f" report_writer={rw}"
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


def _matrix_combo_sync(
    base_wf: dict[str, Any],
    model: str,
    ph: str,
    terminal_agent: str,
    instruction: str,
    report_writer_model: str | None,
    *,
    put_post_delay_sec: float,
    stagger_after_sec: float,
) -> dict[str, Any]:
    """One full matrix cell: PUT clone, POST task, poll to terminal. Runs in a worker thread when async."""
    name = wf_slug(model, ph)
    pj = build_pipeline(base_wf, model, ph, report_writer_model=report_writer_model)
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
        print(f"PUT workflow {name} failed: {e.code} {body[:500]}", file=sys.stderr, flush=True)
        if stagger_after_sec > 0:
            time.sleep(stagger_after_sec)
        return {
            "workflow": name,
            "model": model,
            "prompt_hash": ph,
            "status": "put_failed",
            "detail": str(e),
        }

    if put_post_delay_sec > 0:
        time.sleep(put_post_delay_sec)

    try:
        task = hl.post_task_workflow(MYCROFT_URL, name, instruction, bearer_token=MYCROFT_API_KEY)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        rec = {
            "workflow": name,
            "model": model,
            "prompt_hash": ph,
            "status": "post_failed",
            "detail": f"{e.code} {body[:400]}",
        }
        if stagger_after_sec > 0:
            time.sleep(stagger_after_sec)
        return rec

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
        "query": instruction,
        "first_task_id": tid,
        "run_id": rid,
    }
    if report_writer_model:
        rec["report_writer_model"] = report_writer_model
    if not rid:
        rec["status"] = "no_run_id"
        rec["final_task_id"] = None
    else:
        rw = hl.wait_until_agent_terminal(
            MYCROFT_URL,
            name,
            rid,
            terminal_agent,
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

    print(f"{name} -> {rec.get('status')}", flush=True)
    if stagger_after_sec > 0:
        time.sleep(stagger_after_sec)
    return rec


async def _run_matrix_async(
    base_wf: dict[str, Any],
    chosen: list[str],
    prompt_hashes: list[str],
    terminal_agent: str,
    instruction: str,
    report_writer_model: str | None,
    max_running: int,
    stagger_sec: float,
) -> list[dict[str, Any]]:
    sequential = max_running <= 1
    put_delay = 2.0 if sequential else 0.0
    stagger_after = stagger_sec if sequential else 0.0
    sem = asyncio.Semaphore(max(1, max_running))
    jobs = [(m, ph) for m in chosen for ph in prompt_hashes]

    async def one(model: str, ph: str) -> dict[str, Any]:
        async with sem:
            try:
                return await asyncio.to_thread(
                    _matrix_combo_sync,
                    base_wf,
                    model,
                    ph,
                    terminal_agent,
                    instruction,
                    report_writer_model,
                    put_post_delay_sec=put_delay,
                    stagger_after_sec=stagger_after,
                )
            except Exception as e:
                return {
                    "workflow": wf_slug(model, ph),
                    "model": model,
                    "prompt_hash": ph,
                    "status": "exception",
                    "detail": repr(e),
                }

    return await asyncio.gather(*[one(m, ph) for m, ph in jobs])


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
    ap.add_argument(
        "--max-running",
        type=int,
        default=1,
        metavar="N",
        help="Max concurrent pipelines (default 1 = sequential). >1 uses async; --stagger-sec ignored.",
    )
    ap.add_argument(
        "--instruction",
        default=None,
        metavar="TEXT",
        help="Override task instruction (default: QUIC benchmark). Quote for shell.",
    )
    ap.add_argument(
        "--report-writer-model",
        default=None,
        metavar="M",
        help="Model for report-writer step only (see GOLDEN_MATRIX_REPORT_WRITER_MODEL).",
    )
    ap.add_argument(
        "--matrix-model",
        default=None,
        metavar="M",
        help="Use M for web-search and researcher (single row in the matrix). Skips auto model selection.",
    )
    args = ap.parse_args()
    if args.max_running < 1:
        print("ERROR: --max-running must be >= 1", file=sys.stderr)
        return 1

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

    if args.matrix_model:
        cm = args.matrix_model.strip()
        if not cm:
            print("ERROR: --matrix-model must be non-empty", file=sys.stderr)
            return 1
        chosen = [cm]
        print("Pinned matrix model (--matrix-model):", cm, flush=True)
    else:
        chosen = select_new_models(models, args.max_models)
        print("Selected new models:", chosen or "(none — relax filters or add pulls)", flush=True)

    if args.dry_scan:
        return 0

    if not chosen:
        print("No models to run; exiting.", file=sys.stderr)
        return 1

    prompt_hashes = [h for h in DEFAULT_PROMPT_ORDER if h in GOLDEN_PROMPTS][: args.max_prompts]
    instruction = (args.instruction or "").strip() or COMMON_QUERY
    report_writer_model = (
        (args.report_writer_model or os.environ.get("GOLDEN_MATRIX_REPORT_WRITER_MODEL") or "").strip()
        or None
    )
    print(f"MYCROFT_URL={MYCROFT_URL}", flush=True)
    print("Prompt hashes:", prompt_hashes, flush=True)
    print(f"--max-running={args.max_running}", flush=True)
    print("Instruction:", instruction[:200] + ("…" if len(instruction) > 200 else ""), flush=True)
    if report_writer_model:
        print(f"report-writer model (pinned): {report_writer_model}", flush=True)

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

    runs = asyncio.run(
        _run_matrix_async(
            base_wf,
            chosen,
            prompt_hashes,
            args.terminal_agent,
            instruction,
            report_writer_model,
            args.max_running,
            args.stagger_sec,
        )
    )

    raw_path = os.path.join(out_dir, "golden-model-matrix-raw.json")
    with open(raw_path, "w") as f:
        raw_meta: dict[str, Any] = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "instruction": instruction,
            "default_benchmark_query": COMMON_QUERY,
            "baseline_excluded": sorted(BASELINE_MODELS),
            "source_workflow": args.source_workflow,
            "terminal_agent": args.terminal_agent,
            "max_running": args.max_running,
            "runs": runs,
        }
        if report_writer_model:
            raw_meta["report_writer_model"] = report_writer_model
        if args.matrix_model:
            raw_meta["matrix_model"] = args.matrix_model.strip()
        json.dump(
            raw_meta,
            f,
            indent=2,
        )
    print(f"Wrote {raw_path}", flush=True)

    agent_col = args.terminal_agent.replace("-", " ")
    lines = [
        f"# Golden prompt × new model matrix ({today})",
        "",
        f"Task instruction: *{instruction}*",
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
