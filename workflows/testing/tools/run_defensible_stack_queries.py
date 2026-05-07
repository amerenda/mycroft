#!/usr/bin/env python3
"""Run the defensible per-agent stack (qwen web / mistral researcher / llama report) on three benchmark queries.

Each scenario uses a researcher golden prompt hash tuned to the task plus optional web/report overrides.

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

# Stack aligned with llm-manager.amer.dev catalog + prior eval notes
WEB = "qwen3.5:9b"
RESEARCHER = "mistral-small3.2:24b"
REPORT = "llama3.1:8b"

SCENARIOS: list[dict[str, Any]] = [
    {
        "key": "current-events",
        "title": "Current events — policy and markets (recency + citations)",
        "instruction": (
            "Summarize the most significant U.S. and international economic and monetary policy "
            "developments from roughly the last two weeks. For each bullet, include approximate dates "
            "and cite primary sources (government releases, central banks, regulators, or major wire "
            "reports with named officials)."
        ),
        "researcher_prompt_hash": "3132728a72",
        "web_prompt": (
            "Prioritize sources from roughly the last 14 days. Prefer primary documents (Fed/ECB/BIS, "
            "Treasury, statistical agencies, regulatory filings). Use several independent outlets; "
            "avoid repeating a single narrative without corroboration."
        ),
        "report_prompt": (
            "Return markdown only with: Direct answer (2–4 bullets), Timeline (dated bullets), "
            "Key institutions, Caveats (what is uncertain), Sources (numbered URLs). "
            "Do not invent dates or quotes; omit rather than guess."
        ),
    },
    {
        "key": "technical",
        "title": "Technical — Kubernetes Ingress TLS modes",
        "instruction": (
            "Explain how Kubernetes Ingress controllers typically implement TLS termination versus "
            "TLS pass-through to pods, including when each is appropriate and what operators must "
            "configure (certs, SNI, backend protocols). Cite upstream Kubernetes or ingress-controller "
            "documentation where possible."
        ),
        "researcher_prompt_hash": "8261cadd26",
        "web_prompt": (
            "Prioritize official docs: kubernetes.io, ingress-nginx, Traefik, Gateway API, cloud "
            "provider load-balancer docs. Secondary: well-known engineering blogs only if they link "
            "to specs or upstream issues."
        ),
        "report_prompt": (
            "Markdown only: Overview, Termination vs pass-through (comparison), Configuration checklist, "
            "Operational caveats, References (numbered links). No JSON or tool-call shaped output."
        ),
    },
    {
        "key": "insights",
        "title": "Insights — evidence on AI coding assistants and team productivity",
        "instruction": (
            "What are the strongest evidence-backed claims about whether AI coding assistants improve "
            "software team productivity, quality, or velocity? Cover both supportive and null findings. "
            "Call out study designs, confounders, and replication gaps. Cite specific papers, reports, "
            "or controlled studies where available."
        ),
        "researcher_prompt_hash": "d7d5d34d8f",
        "web_prompt": (
            "Look for peer-reviewed work, arXiv papers with evaluation sections, industry studies that "
            "publish methodology, and meta-commentary from reputable research labs. Prefer 2021–2026; "
            "flag anecdotal or vendor-only marketing."
        ),
        "report_prompt": (
            "Markdown: Executive summary, Claims with evidence strength (High/Medium/Low), "
            "Methodological weaknesses, Conflicts of interest or vendor bias, Open questions, "
            "Sources. Prefer omission to speculation."
        ),
    },
]


def _workflow_name(scenario_key: str, researcher_hash: str) -> str:
    sig = hashlib.sha256(f"{scenario_key}|{researcher_hash}".encode()).hexdigest()[:6]
    safe = re.sub(r"[^a-z0-9-]+", "-", scenario_key.lower()).strip("-")[:20]
    name = f"rn-dsq-{safe}-{researcher_hash[:4]}-{sig}"
    if len(name) > 63:
        name = name[:63].rstrip("-")
    return name


def main() -> int:
    today = str(date.today())
    out_root = Path(hl.testing_output_dir(_TOOLS_DIR, "defensible-stack-queries", today))
    out_root.mkdir(parents=True, exist_ok=True)

    print(f"MYCROFT_URL={MYCROFT_URL}", flush=True)
    print(f"Stack web={WEB} researcher={RESEARCHER} report={REPORT}", flush=True)

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

    results: list[dict[str, Any]] = []
    for i, sc in enumerate(SCENARIOS):
        wn = _workflow_name(sc["key"], sc["researcher_prompt_hash"])
        print(f"\n=== [{i + 1}/3] {sc['key']} -> {wn} ===", flush=True)
        pj = gm.build_pipeline_per_agent(
            base_wf,
            web_search=WEB,
            researcher=RESEARCHER,
            report_writer=REPORT,
            prompt_hash=sc["researcher_prompt_hash"],
            description_tag=sc["title"],
            web_prompt_override=sc["web_prompt"],
            report_prompt_override=sc["report_prompt"],
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
            "researcher_prompt_hash": sc["researcher_prompt_hash"],
            "models": {"web_search": WEB, "researcher": RESEARCHER, "report_writer": REPORT},
            "workflow": wn,
            **{k: tail.get(k) for k in ("status", "detail", "first_task_id", "run_id", "final_task_id", "summary_preview", "error") if k in tail},
        }
        results.append(rec)
        print(f"Done {sc['key']}: status={rec.get('status')}", flush=True)

    raw_path = out_root / "defensible-stack-queries-raw.json"
    summary_path = out_root / "defensible-stack-queries-summary.md"
    meta = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mycroft_url": MYCROFT_URL,
        "source_workflow": SOURCE_WORKFLOW,
        "stack": {"web_search": WEB, "researcher": RESEARCHER, "report_writer": REPORT},
        "runs": results,
    }
    raw_path.write_text(json.dumps(meta, indent=2) + "\n")
    print(f"\nWrote {raw_path}", flush=True)

    lines = [
        f"# Defensible stack — three queries ({today})",
        "",
        f"**Models:** web `{WEB}`, researcher `{RESEARCHER}`, report `{REPORT}`",
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
            lines.append(r["summary_preview"][:2500])
            lines.append("")
    summary_path.write_text("\n".join(lines) + "\n")
    print(f"Wrote {summary_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
