#!/usr/bin/env python3
"""
Run five per-agent model mixes through Mycroft (one shared search query), then
write a comparison table (status + summary preview).

Uses the same five configs as tests/test_research_new_baseline_regression.py
(`test_next_five_recommended_runs_are_defined_for_matrix_harness`).

Environment:
  MYCROFT_URL     Coordinator base URL (required for real runs)
  LLM_MANAGER_URL Optional; if set, GET /api/models there to warn on unknown model names
  MYCROFT_API_KEY Optional Bearer token for coordinator
  GOLDEN_MATRIX_TIMEOUT  Seconds to wait per pipeline (default 7200)

Example:
  MYCROFT_URL=http://127.0.0.1:18088 python workflows/testing/tools/run_per_agent_model_compare.py
  MYCROFT_URL=... python .../run_per_agent_model_compare.py --max-running 5   # all five in parallel
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
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

MYCROFT_URL = os.environ.get("MYCROFT_URL", "http://127.0.0.1:8080").rstrip("/")
LLM_MANAGER_URL = os.environ.get("LLM_MANAGER_URL", "").rstrip("/")
MYCROFT_API_KEY = os.environ.get("MYCROFT_API_KEY", "").strip()
SOURCE_WORKFLOW = "research-new"
RUN_ID_WAIT = 180
PER_RUN_TIMEOUT = int(os.environ.get("GOLDEN_MATRIX_TIMEOUT", "7200"))


def _load_golden():
    root = Path(__file__).resolve().parent.parent.parent.parent
    path = root / "workflows" / "testing" / "tools" / "golden_model_matrix.py"
    spec = importlib.util.spec_from_file_location("golden_model_matrix", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("golden_model_matrix not found")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gm = _load_golden()


COMPARE_RUNS: list[dict[str, Any]] = [
    {
        "id": 1,
        "web_search": "qwen3.5:9b",
        "researcher": "ministral-3:14b",
        "report_writer": "qwen3.5:9b",
        "researcher_prompt_hash": "8261cadd26",
        "note": "Best baseline prompt (9) + split models",
    },
    {
        "id": 2,
        "web_search": "qwen3.5:9b",
        "researcher": "ministral-3:14b",
        "report_writer": "llama3.1:8b",
        "researcher_prompt_hash": "fdd9ef09fd",
        "note": "Citation analyst prompt + llama final",
    },
    {
        "id": 3,
        "web_search": "qwen3.5:9b",
        "researcher": "qwen3.5:9b",
        "report_writer": "qwen3.5:9b",
        "researcher_prompt_hash": "5bc1060d34",
        "note": "All-qwen + fetch-first prompt (best qwen completed 8)",
    },
    {
        "id": 4,
        "web_search": "qwen3.5:9b",
        "researcher": "ministral-3:14b",
        "report_writer": "qwen3.5:9b",
        "researcher_prompt_hash": "84612fc2d0",
        "note": "Structured skeleton + split",
    },
    {
        "id": 5,
        "web_search": "qwen3.5:9b",
        "researcher": "ministral-3:14b",
        "report_writer": "qwen3.5:9b",
        "researcher_prompt_hash": "d7d5d34d8f",
        "note": "Strict anti-hallucination + split",
    },
]


def _catalog_model_names() -> set[str]:
    out: set[str] = set()
    bases = [b for b in ([LLM_MANAGER_URL] if LLM_MANAGER_URL else []) + [MYCROFT_URL] if b]
    for base in bases:
        try:
            raw = hl.http_json("GET", base, "/api/models", None, timeout=30.0, bearer_token=MYCROFT_API_KEY)
            if isinstance(raw, list):
                for m in raw:
                    n = (m.get("name") or m.get("id") or "").strip()
                    if n:
                        out.add(n)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
            continue
    return out


def wf_name(run_id: int) -> str:
    return f"rn-cmp{run_id}"


def _compare_row_sync(
    row: dict[str, Any],
    base_wf: dict[str, Any],
    catalog: set[str],
    terminal_agent: str,
    dry_run: bool,
    *,
    put_post_delay_sec: float,
    stagger_after_sec: float,
) -> dict[str, Any]:
    wn = wf_name(row["id"])
    models = (row["web_search"], row["researcher"], row["report_writer"])
    missing = [m for m in models if catalog and m not in catalog]
    if missing:
        print(f"WARN {wn}: model(s) not in catalog {missing} — continuing anyway", flush=True)

    pj = gm.build_pipeline_per_agent(
        base_wf,
        web_search=row["web_search"],
        researcher=row["researcher"],
        report_writer=row["report_writer"],
        prompt_hash=row["researcher_prompt_hash"],
        description_tag=row.get("note") or "",
    )
    try:
        hl.put_workflow(
            MYCROFT_URL,
            wn,
            content=base_wf.get("content") or "",
            pipeline_json=pj,
            bearer_token=MYCROFT_API_KEY,
        )
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        if stagger_after_sec > 0:
            time.sleep(stagger_after_sec)
        return {
            "workflow": wn,
            "id": row["id"],
            "status": "put_failed",
            "detail": f"{e.code} {body[:400]}",
        }

    rec: dict[str, Any] = {
        "workflow": wn,
        "id": row["id"],
        "web_search": row["web_search"],
        "researcher": row["researcher"],
        "report_writer": row["report_writer"],
        "researcher_prompt_hash": row["researcher_prompt_hash"],
        "note": row.get("note"),
        "query": gm.COMMON_QUERY,
    }
    if dry_run:
        rec["status"] = "dry_run_put_only"
        if stagger_after_sec > 0:
            time.sleep(stagger_after_sec)
        return rec

    if put_post_delay_sec > 0:
        time.sleep(put_post_delay_sec)

    try:
        task = hl.post_task_workflow(MYCROFT_URL, wn, gm.COMMON_QUERY, bearer_token=MYCROFT_API_KEY)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        rec["status"] = "post_failed"
        rec["detail"] = f"{e.code} {body[:400]}"
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
    rec["first_task_id"] = tid
    rec["run_id"] = rid
    if not rid:
        rec["status"] = "no_run_id"
    else:
        rw = hl.wait_until_agent_terminal(
            MYCROFT_URL,
            wn,
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
            summ = (res.get("summary") or "") if isinstance(res, dict) else ""
            rec["summary_chars"] = len(summ)
            rec["summary_preview"] = summ[:700]
            rec["error"] = res.get("error")

    print(f"{wn} -> {rec.get('status')}", flush=True)
    if stagger_after_sec > 0:
        time.sleep(stagger_after_sec)
    return rec


async def _run_compare_async(
    base_wf: dict[str, Any],
    catalog: set[str],
    terminal_agent: str,
    dry_run: bool,
    max_running: int,
    stagger_sec: float,
) -> list[dict[str, Any]]:
    sequential = max_running <= 1
    put_delay = 2.0 if sequential and not dry_run else 0.0
    stagger_after = stagger_sec if sequential else 0.0
    sem = asyncio.Semaphore(max(1, max_running))

    async def one(row: dict[str, Any]) -> dict[str, Any]:
        async with sem:
            try:
                return await asyncio.to_thread(
                    _compare_row_sync,
                    row,
                    base_wf,
                    catalog,
                    terminal_agent,
                    dry_run,
                    put_post_delay_sec=put_delay,
                    stagger_after_sec=stagger_after,
                )
            except Exception as e:
                return {
                    "workflow": wf_name(row["id"]),
                    "id": row["id"],
                    "status": "exception",
                    "detail": repr(e),
                }

    return await asyncio.gather(*[one(row) for row in COMPARE_RUNS])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workflow-dir", default="research-new", help="Subfolder under workflows/testing/")
    ap.add_argument("--source-workflow", default=SOURCE_WORKFLOW, help="Base workflow name in coordinator")
    ap.add_argument(
        "--terminal-agent",
        default="report-writer",
        help="Poll until this agent_type is completed or failed",
    )
    ap.add_argument("--dry-run", action="store_true", help="PUT workflows only, no POST /api/tasks")
    ap.add_argument("--stagger-sec", type=float, default=20.0)
    ap.add_argument(
        "--max-running",
        type=int,
        default=1,
        metavar="N",
        help="Max concurrent compare runs (default 1). >1 uses async; --stagger-sec ignored.",
    )
    args = ap.parse_args()
    if args.max_running < 1:
        print("ERROR: --max-running must be >= 1", file=sys.stderr)
        return 1

    today = str(date.today())
    out_dir = Path(hl.testing_output_dir(str(Path(__file__).resolve().parent), args.workflow_dir, today))
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"MYCROFT_URL={MYCROFT_URL}", flush=True)
    catalog = _catalog_model_names()
    if catalog:
        print(f"Catalog: {len(catalog)} model name(s) from /api/models", flush=True)

    try:
        base_wf = hl.http_json(
            "GET",
            MYCROFT_URL,
            f"/api/workflows/{args.source_workflow}",
            None,
            timeout=60.0,
            bearer_token=MYCROFT_API_KEY,
        )
    except (urllib.error.URLError, OSError) as e:
        print(f"ERROR: cannot reach coordinator: {e}", file=sys.stderr)
        return 2
    if not isinstance(base_wf, dict) or "pipeline_json" not in base_wf:
        print("ERROR: GET workflow returned unexpected body", file=sys.stderr)
        return 1

    print(f"--max-running={args.max_running}", flush=True)
    runs = asyncio.run(
        _run_compare_async(
            base_wf,
            catalog,
            args.terminal_agent,
            args.dry_run,
            args.max_running,
            args.stagger_sec,
        )
    )

    raw_path = out_dir / "per-agent-model-compare-raw.json"
    with open(raw_path, "w") as f:
        json.dump(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "mycroft_url": MYCROFT_URL,
                "common_query": gm.COMMON_QUERY,
                "source_workflow": args.source_workflow,
                "terminal_agent": args.terminal_agent,
                "max_running": args.max_running,
                "runs": runs,
            },
            f,
            indent=2,
        )
    print(f"Wrote {raw_path}", flush=True)

    lines = [
        f"# Per-agent model compare — single query ({today})",
        "",
        f"*Query:* {gm.COMMON_QUERY}",
        "",
        "| # | workflow | web | researcher | report | prompt | status | summary chars |",
        "|---:|---|---|---|---|---|---:|---:|",
    ]
    for r in runs:
        lines.append(
            "| {id} | `{wf}` | `{w}` | `{res}` | `{rep}` | `{ph}` | {st} | {sc} |".format(
                id=r.get("id", "-"),
                wf=r.get("workflow", "-"),
                w=r.get("web_search", "-"),
                res=r.get("researcher", "-"),
                rep=r.get("report_writer", "-"),
                ph=r.get("researcher_prompt_hash", "-"),
                st=r.get("status", "-"),
                sc=r.get("summary_chars", "-"),
            )
        )
    lines.append("")
    lines.append("## Notes")
    for r in runs:
        if r.get("note"):
            lines.append(f"- **{r.get('workflow')}**: {r['note']}")
        if r.get("error"):
            lines.append(f"- **{r.get('workflow')}** error: `{str(r['error'])[:200]}`")
    lines.append("")
    lines.append("## Preview (truncated)")
    for r in runs:
        if r.get("summary_preview"):
            lines.append(f"### {r.get('workflow')}")
            lines.append("")
            lines.append(r["summary_preview"][:1200])
            lines.append("")

    md_path = out_dir / "per-agent-model-compare.md"
    md_path.write_text("\n".join(lines) + "\n")
    print(f"Wrote {md_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
