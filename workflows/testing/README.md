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

```bash
export LLM_MANAGER_URL=https://<llm-manager-host>
export MYCROFT_URL=http://127.0.0.1:<coordinator-port-forward>
# export MYCROFT_API_KEY=...   # if ingress requires Bearer auth
python workflows/testing/tools/golden_model_matrix.py
python workflows/testing/tools/golden_model_matrix.py --dry-scan   # catalog only
```

Optional: `--workflow-dir` (default `research-new`), `--source-workflow` (default `research-new`), `--terminal-agent` (default `report-writer`), `--max-models`, `--max-prompts`, `--stagger-sec`, and env `GOLDEN_MATRIX_TIMEOUT` (seconds per pipeline).

## Per-agent model compare (one query, five API runs)

`tools/run_per_agent_model_compare.py` registers `rn-cmp1` … `rn-cmp5` with **different models per agent** (web / researcher / report-writer), runs the **same QUIC instruction** on each, polls `report-writer`, and writes `research-new/<date>/per-agent-model-compare-raw.json` and `per-agent-model-compare.md`. Set `LLM_MANAGER_URL` to warn if a model name is missing from `/api/models`. `--dry-run` only PUTs workflows.

```bash
export MYCROFT_URL=http://127.0.0.1:<coordinator-port-forward>
# export MYCROFT_API_KEY=...   # if your ingress requires Bearer auth
python workflows/testing/tools/run_per_agent_model_compare.py
```

Same optional flags as the golden matrix: `--workflow-dir`, `--source-workflow`, `--terminal-agent`.
