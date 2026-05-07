#!/usr/bin/env python3
"""Five-query fetch-first benchmark using the best-performing stack from 2026-05-07 evals.

Stack: qwen3.5:9b web-search, magistral:24b researcher, qwen3.5:9b report-writer.
Prompt: 5bc1060d34 (fetch-first grounding — highest factual density in compare run 3).

Queries span five categories: product-rec, current-events, factual-technical,
research-synthesis, comparative-tech.

Environment (same as golden_model_matrix):
  MYCROFT_URL, MYCROFT_API_KEY, GOLDEN_MATRIX_TIMEOUT
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

_TOOLS_DIR = str(Path(__file__).resolve().parent)
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)
import golden_model_matrix as gm  # noqa: E402
import harness_lib as hl  # noqa: E402

MYCROFT_URL = os.environ.get("MYCROFT_URL", "http://127.0.0.1:8080").rstrip("/")
MYCROFT_API_KEY = os.environ.get("MYCROFT_API_KEY", "").strip()
SOURCE_WORKFLOW = "research-new"
TERMINAL_AGENT = "report-writer"

WEB = "qwen3.5:9b"
RESEARCHER = "magistral:24b"
REPORT = "qwen3.5:9b"
PROMPT_HASH = "5bc1060d34"  # fetch-first grounding prompt

SCENARIOS: list[dict[str, Any]] = [
    {
        "key": "product-rec-ssd",
        "title": "Product rec — best NVMe SSDs for a home server in 2025",
        "instruction": (
            "What are the best NVMe SSDs for a home server or NAS in 2025? "
            "Compare the top options under $300 by sequential read/write speed, random 4K IOPS, "
            "endurance rating (DWPD or TBW), and price-per-TB. Include at least one consumer TLC "
            "option and one enterprise-lite option. Note any known reliability concerns or firmware "
            "issues from user reports."
        ),
        "web_prompt": (
            "Search for recent benchmarks and reviews (2024–2025) from Tom's Hardware, AnandTech, "
            "STH (ServeTheHome), and Reddit r/homelab or r/DataHoarder. Prefer sources with measured "
            "specs rather than manufacturer claims. Fetch the actual benchmark pages."
        ),
        "report_prompt": (
            "Markdown only. Sections: Executive summary (1 paragraph), Comparison table "
            "(model | capacity | seq R/W MB/s | 4K IOPS | TBW | $/TB | notes), "
            "Top pick with rationale, Reliability caveats, Sources (numbered URLs). "
            "No filler — omit rather than guess any spec."
        ),
    },
    {
        "key": "current-events-ai",
        "title": "Current events — major AI model releases and benchmark results (past 30 days)",
        "instruction": (
            "What major AI model releases, capability announcements, or independent benchmark "
            "results have been published in the past 30 days? For each, include: model name and "
            "organization, key capability claims, any independent evaluation or reproduction, "
            "and approximate release date. Cover both open-weight and closed models."
        ),
        "web_max_iterations": 30,
        "web_prompt": (
            "Search for AI model releases in 2026. Check Hugging Face announcements, arXiv, "
            "official model cards, and tech press (The Verge, Ars Technica, VentureBeat AI). "
            "Prefer primary sources (model cards, official blogs, papers) over aggregators. "
            "Fetch and read the actual announcement pages."
        ),
        "report_prompt": (
            "Markdown only. Sections: Timeline (chronological bullets with dates), "
            "Model-by-model breakdown (name | org | type | key claims | independent eval), "
            "Notable trends, Caveats (what is unverified), Sources (numbered URLs). "
            "Use approximate dates if exact dates are unclear; omit rather than fabricate."
        ),
    },
    {
        "key": "factual-ebpf",
        "title": "Factual/technical — how Linux eBPF works (verifier, JIT, maps, use cases)",
        "instruction": (
            "Explain how the Linux kernel's eBPF subsystem works. Cover: the verifier and its "
            "safety guarantees, JIT compilation to native code, BPF map types and their semantics, "
            "and the major production use cases (networking/XDP, observability, security/LSM). "
            "Include kernel version milestones and cite upstream documentation or LWN.net articles."
        ),
        "web_prompt": (
            "Prioritize: kernel.org eBPF docs, Cilium eBPF library docs, LWN.net eBPF articles, "
            "Facebook/Meta and Cloudflare engineering blogs on eBPF in production. "
            "Fetch the actual documentation pages to get precise version numbers and semantics."
        ),
        "report_prompt": (
            "Markdown only. Sections: Overview, Verifier (what it checks and guarantees), "
            "JIT compilation, Map types (table: name | semantics | typical use), "
            "Production use cases with examples, Kernel version milestones, References (numbered). "
            "Include code-level detail where it clarifies behavior."
        ),
    },
    {
        "key": "research-synthesis-sleep",
        "title": "Research synthesis — sleep deprivation effects on cognition and health",
        "instruction": (
            "What does published research say about the dose-response relationship between sleep "
            "deprivation and cognitive performance, immune function, and metabolic health? "
            "Distinguish acute (24h+) from chronic partial restriction (5–6h/night). "
            "Cover study designs, effect sizes where reported, and replication gaps. "
            "Cite specific papers or meta-analyses."
        ),
        "web_prompt": (
            "Search PubMed, Google Scholar, and arXiv for sleep deprivation RCTs and meta-analyses "
            "(2015–2026). Prefer studies with objective cognitive measures (PVT, DSST), "
            "biomarker outcomes (cytokines, glucose, insulin), or large N. "
            "Fetch and read abstracts and methods sections."
        ),
        "report_prompt": (
            "Markdown only. Sections: Executive summary, Cognitive effects (by deprivation type), "
            "Immune and metabolic effects, Evidence quality table "
            "(claim | study design | effect size | N | replication), "
            "Methodological weaknesses, Open questions, Sources (numbered). "
            "Label evidence strength: High/Medium/Low."
        ),
    },
    {
        "key": "comparative-db-acid",
        "title": "Comparative — PostgreSQL vs MySQL vs SQLite: ACID, MVCC, and when to choose each",
        "instruction": (
            "Compare PostgreSQL, MySQL/MariaDB (InnoDB), and SQLite on: ACID compliance "
            "implementation details, WAL/redo log mechanics, concurrency model (MVCC vs locking), "
            "isolation levels supported, and practical performance trade-offs. When is each the "
            "right choice for a new project? Cite official documentation."
        ),
        "web_prompt": (
            "Prioritize official docs: postgresql.org, dev.mysql.com, sqlite.org. "
            "Also check reputable engineering posts that cite specific version behavior "
            "(Percona blog, Brandur Leach's postgres articles, SQLite documentation FAQ). "
            "Fetch actual docs pages for isolation level tables and WAL descriptions."
        ),
        "report_prompt": (
            "Markdown only. Sections: ACID implementation comparison table "
            "(atomicity | consistency | isolation | durability — row per DB), "
            "Concurrency model detail, Isolation levels table, WAL mechanics, "
            "Decision guide (when to choose each), References (numbered URLs). "
            "Be precise about version-specific behavior."
        ),
    },
]


def _workflow_name(scenario_key: str) -> str:
    sig = hashlib.sha256(f"{scenario_key}|{PROMPT_HASH}".encode()).hexdigest()[:6]
    safe = re.sub(r"[^a-z0-9-]+", "-", scenario_key.lower()).strip("-")[:22]
    name = f"rn-ff-{safe}-{sig}"
    if len(name) > 63:
        name = name[:63].rstrip("-")
    return name


def main() -> int:
    today = str(date.today())
    out_root = Path(hl.testing_output_dir(_TOOLS_DIR, "fetch-first-benchmark", today))
    out_root.mkdir(parents=True, exist_ok=True)

    print(f"MYCROFT_URL={MYCROFT_URL}", flush=True)
    print(f"Stack web={WEB} researcher={RESEARCHER} report={REPORT} prompt={PROMPT_HASH}", flush=True)

    try:
        base_wf = hl.http_json(
            "GET",
            MYCROFT_URL,
            f"/api/workflows/{SOURCE_WORKFLOW}",
            None,
            timeout=60.0,
            bearer_token=MYCROFT_API_KEY,
        )
    except Exception as e:
        print(f"ERROR: GET workflow: {e}", file=sys.stderr)
        return 2
    if not isinstance(base_wf, dict) or "pipeline_json" not in base_wf:
        print("ERROR: unexpected GET workflow body", file=sys.stderr)
        return 1

    n = len(SCENARIOS)
    results: list[dict[str, Any]] = []
    for i, sc in enumerate(SCENARIOS):
        wn = _workflow_name(sc["key"])
        print(f"\n=== [{i + 1}/{n}] {sc['key']} -> {wn} ===", flush=True)
        pj = gm.build_pipeline_per_agent(
            base_wf,
            web_search=WEB,
            researcher=RESEARCHER,
            report_writer=REPORT,
            prompt_hash=PROMPT_HASH,
            description_tag=sc["title"],
            web_prompt_override=sc.get("web_prompt", ""),
            report_prompt_override=sc.get("report_prompt", ""),
            web_max_iterations=sc.get("web_max_iterations"),
        )
        tail = gm.execute_workflow_run_sync(
            base_wf,
            wn,
            pj,
            TERMINAL_AGENT,
            sc["instruction"],
            put_post_delay_sec=2.0,
            stagger_after_sec=4.0,
        )
        rec = {
            "scenario_key": sc["key"],
            "title": sc["title"],
            "instruction": sc["instruction"],
            "prompt_hash": PROMPT_HASH,
            "models": {"web_search": WEB, "researcher": RESEARCHER, "report_writer": REPORT},
            "workflow": wn,
            **{k: tail.get(k) for k in ("status", "detail", "first_task_id", "run_id", "final_task_id", "summary_preview", "error") if k in tail},
        }
        results.append(rec)
        print(f"Done {sc['key']}: status={rec.get('status')}", flush=True)

    raw_path = out_root / "fetch-first-benchmark-raw.json"
    summary_path = out_root / "fetch-first-benchmark-summary.md"
    meta = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mycroft_url": MYCROFT_URL,
        "source_workflow": SOURCE_WORKFLOW,
        "prompt_hash": PROMPT_HASH,
        "stack": {"web_search": WEB, "researcher": RESEARCHER, "report_writer": REPORT},
        "runs": results,
    }
    raw_path.write_text(json.dumps(meta, indent=2) + "\n")
    print(f"\nWrote {raw_path}", flush=True)

    lines = [
        f"# Fetch-first benchmark — five queries ({today})",
        "",
        f"**Stack:** web `{WEB}`, researcher `{RESEARCHER}`, report `{REPORT}`  ",
        f"**Prompt:** `{PROMPT_HASH}` (fetch-first grounding)",
        "",
        "| # | scenario | workflow | status | preview |",
        "|---:|---|---|---|---|",
    ]
    for i, r in enumerate(results, start=1):
        prev = (r.get("summary_preview") or "").replace("\n", " ")[:120]
        lines.append(
            f"| {i} | `{r.get('scenario_key')}` | `{r.get('workflow')}` | {r.get('status')} | {prev} |"
        )
    lines.append("")
    for r in results:
        lines.append(f"## {r.get('scenario_key')}")
        lines.append("")
        lines.append(f"*{r.get('instruction')}*")
        lines.append("")
        if r.get("summary_preview"):
            lines.append(r["summary_preview"][:3000])
            lines.append("")
    summary_path.write_text("\n".join(lines) + "\n")
    print(f"Wrote {summary_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
