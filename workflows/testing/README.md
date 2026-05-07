# Workflow testing harness

This folder stores repeatable end-to-end workflow test runs and outputs.

## Coordinator seed workflow

Fresh databases load [`workflows/research-new.yaml`](../research-new.yaml) (via `seed_from_filesystem`) so `research-new` exists with `pipeline_json.steps` before you use the API or harness scripts. The row is only inserted when missing; production DBs keep UI-edited definitions.

## Shared library

[`tools/harness_lib.py`](tools/harness_lib.py) centralizes HTTP (`http_json` with optional Bearer auth), model selection helpers, workflow slug naming, `PUT /api/workflows`, `POST /api/tasks`, run-id polling, and waiting for a terminal agent on a run. [`tools/golden_model_matrix.py`](tools/golden_model_matrix.py) and [`tools/run_per_agent_model_compare.py`](tools/run_per_agent_model_compare.py) use it; new workflows can reuse the same primitives with different `--source-workflow`, `--workflow-dir`, `--terminal-agent`, and query constants.

## research-new test procedure

1. Pick one common query and decide N variants (prompt and/or model).
2. Create workflow variants (clone `research-new`) with per-variant step overrides.
3. Submit each variant via `POST /api/tasks` with `workflow` + common `instruction`.
4. Poll `GET /api/workflows/{name}/runs` until `report-writer` is terminal.
5. Collect `GET /api/tasks/{id}` result summary and status.
6. Score each run on a 0-10 rubric (quality + grounding + format adherence).
7. Save outputs under `workflows/testing/research-new/YYYY-MM-DD/`.

## Required output files per run date

- `results.md`
- `prompts.md` (and `promts.md` alias for compatibility)
- `raw_results.json` (optional but recommended)

## `results.md` format

```markdown
Common Query: *<query>*

| agent | model | prompt hash | result score 0-10 |
|---|---|---|---|
| research-new | <model> | <hash> | <score> |
```

## `prompts.md` / `promts.md` format

Use one section per prompt hash and include full prompt text used for that variant.

## Golden prompts × new models (script)

`tools/golden_model_matrix.py` calls llm-manager `GET /api/models` (no auth), writes `<workflow-dir>/<date>/model-scan.json`, picks a few non-baseline chat models that report `fits`, clones the source workflow per `(model, prompt_hash)` with the same model on every pipeline step and `prompt_override` on the researcher step, submits the standard QUIC query from prior runs, and writes `golden-model-matrix-raw.json` plus `golden-model-matrix-results.md`.

**Model per agent:** set both `--web-search-model` and `--researcher-model` and a report-writer model via `--report-writer-model` (or `GOLDEN_MATRIX_REPORT_WRITER_MODEL`). That runs one fixed triple × each selected golden prompt (no matrix of new models from llm-manager; `--matrix-model` is not allowed). Raw JSON includes `matrix_mode: "per_agent"` and `per_agent_models`.

```bash
export LLM_MANAGER_URL=https://<llm-manager-host>
export MYCROFT_URL=http://127.0.0.1:<coordinator-port-forward>
# export MYCROFT_API_KEY=...   # if ingress requires Bearer auth
python workflows/testing/tools/golden_model_matrix.py
python workflows/testing/tools/golden_model_matrix.py --dry-scan   # catalog only
```

Optional: `--workflow-dir` (default `research-new`), `--source-workflow` (default `research-new`), `--terminal-agent` (default `report-writer`), `--max-models`, `--max-prompts`, `--stagger-sec`, `--max-running N` (concurrent pipelines via asyncio; default `1` = sequential; when `N>1`, `--stagger-sec` is ignored for faster / load-test runs), `--instruction "…"` (override the default QUIC task text; use a separate `--workflow-dir` if you change the instruction on the same calendar day so `model-scan.json` does not collide), `--report-writer-model M` (keep web-search + researcher on the matrix model but pin **report-writer** to `M` — avoids coder-tuned matrix models emitting fenced JSON / tool-call shaped text as the final report), env `GOLDEN_MATRIX_REPORT_WRITER_MODEL` (same as the flag), and env `GOLDEN_MATRIX_TIMEOUT` (seconds per pipeline).

## Report-writer sweep (same prompt, vary final model)

`tools/report_writer_sweep.py` runs `golden_model_matrix.py` once per **`SWEEP_REPORT_WRITERS`** (comma list) with a fixed **`SWEEP_MATRIX_MODEL`** for web+researcher, **`--max-prompts 1`**, then picks the best `summary_preview` via a small heuristic (penalizes fenced JSON / tool-call shapes; rewards markdown headings and URLs). It then runs a **confirmation** pass: same matrix model, **four** golden researcher prompts, **`--max-running 2`**, with the winning report-writer.

## Defensible stack — three benchmark queries (script)

`tools/run_defensible_stack_queries.py` runs **web-search `qwen3.5:9b`**, **researcher `mistral-small3.2:24b`**, **report-writer `llama3.1:8b`** on three instructions (current events, technical Ingress/TLS, AI productivity evidence). Each scenario uses a tailored **researcher** golden hash plus **web** and **report** `prompt_override` text. Outputs: `workflows/testing/defensible-stack-queries/<date>/defensible-stack-queries-raw.json` and `defensible-stack-queries-summary.md`.

```bash
export MYCROFT_URL=https://mycroft.example.com
# export MYCROFT_API_KEY=...
uv run python -u workflows/testing/tools/run_defensible_stack_queries.py
```

## Per-agent model compare (one query, five API runs)

`tools/run_per_agent_model_compare.py` registers `rn-cmp1` … `rn-cmp5` with **different models per agent** (web / researcher / report-writer), runs the **same QUIC instruction** on each, polls `report-writer`, and writes `research-new/<date>/per-agent-model-compare-raw.json` and `per-agent-model-compare.md`. Set `LLM_MANAGER_URL` to warn if a model name is missing from `/api/models`. `--dry-run` only PUTs workflows.

```bash
export MYCROFT_URL=http://127.0.0.1:<coordinator-port-forward>
# export MYCROFT_API_KEY=...   # if your ingress requires Bearer auth
python workflows/testing/tools/run_per_agent_model_compare.py
```

Same optional flags as the golden matrix: `--workflow-dir`, `--source-workflow`, `--terminal-agent`, `--max-running` (e.g. `5` to launch all compare rows at once).
