# Mycroft

AI agent platform running on k3s. Accepts tasks via the web UI, runs agents as ephemeral Argo Workflow pods, stores knowledge in pgvector, and produces reports.

---

## Architecture

```
Browser
  │
  ▼
Coordinator (FastAPI)
├── Task Manager  ─────────────────► PostgreSQL (agent-kb)
├── Argo Submitter ────────────────► k3s / Argo Workflows
├── Telegram Bot (notify-only)
└── Report Store
                                              │
                                             pods
                                              │
                                         Agent Runtime
                                         ├── Tool loop
                                         ├── LLM calls ─► llm-manager → Ollama / vLLM
                                         └── KB writes ─► agent-kb (pgvector)
```

### Components

| Directory | Purpose |
|-----------|---------|
| `coordinator/` | FastAPI service: task API, Telegram notifications, Argo submission, Forge runner, report storage |
| `runtime/` | Thin agent loop that runs inside Argo Workflow pods |
| `agents/` | Agent definitions: `manifest.yaml` + `prompts.py` per agent type |
| `common/` | Shared libraries: KB client, LLM client, config, models |
| `frontend/` | Single-page web UI |
| `workflows/` | Argo WorkflowTemplate YAMLs (legacy; dynamic workflows now live in DB) |

### Agents

| Agent | Model | Purpose | Trigger |
|-------|-------|---------|---------|
| `researcher` | qwen3:14b | Web research — gathers sources | API, UI, pipeline step |
| `web-search` | qwen3.5:9b | Lightweight web search sub-agent | Pipeline step |
| `writer` | llama3.1:8b | Synthesizes gathered findings into a report | Pipeline write phase |
| `report-writer` | llama3.1:8b | Formats and writes final polished report | Pipeline step, API |
| `extractor` | qwen3:14b | Extracts structured data from text | Pipeline, API |
| `coder` | qwen3:14b | Clone repo, implement changes, open PR | API, UI |

### Workflows (Pipelines)

| Workflow | Description |
|----------|-------------|
| `research-quick` | Single researcher agent, no pipeline |
| `research-regular` | researcher (gather) → writer, ~8 min |
| `research-deep` | researcher (gather) → researcher (review) → writer |
| `research-new` | web-search → researcher → report-writer (3-step, DB-defined) |
| `coder` | Coder agent, opens a PR |
| Custom | Multi-step pipelines built in the Workflows editor |

---

## Web UI

Served at the coordinator root. Tabs:

- **Test Runner** — submit tasks, configure model/workflow/tools, view live trace
- **Agents** — edit agent manifests, system prompts, resources, run agents in isolation
- **Workflows** — build and edit multi-step pipelines
- **Tools** — manage tool schemas (OpenAI function-calling format)
- **Reports** — browse and read AI-generated reports, jump to source task trace
- **Logs** — live coordinator log stream with level/logger/text filters

---

## Knowledge Base (agent-kb)

PostgreSQL + pgvector, running on the Mac Mini. All persistent state lives here.

### Memory Tiers

| Tier | TTL | Namespace | Purpose |
|------|-----|-----------|---------|
| **Short-term** | 7 days | `/runs/{run_id}/` | Pipeline run data — expires automatically |
| **Long-term** | Permanent | `/agents/*/results/`, `/tasks/`, `/research/` | Results, history, shared knowledge |

Short-term records carry an `expires_at` timestamp. The coordinator runs hourly cleanup. `ensure_schema()` at startup adds the column idempotently if it doesn't exist.

### Key Paths

```
/agents/{name}/inbox/{task_id}      task instructions
/tasks/{task_id}/conversation       full conversation history (JSON)
/agents/{name}/results/{task_id}    final agent output (permanent)
/notifications/{user}/{task_id}     errors / alerts
/runs/{run_id}/original             original user request for a pipeline run (7d TTL)
/runs/{run_id}/step-{n}/output      full output of pipeline step N (7d TTL)
/runs/{run_id}/scratch              shared notepad for all agents in the run (7d TTL)
/research, /wiki                    shared read-only reference context
/skills/                            (planned) shared skill knowledge blocks
```

### Pipeline Context Flow

Context between pipeline steps flows through KB — not Argo args. Before the first LLM call, the runner reads each scope listed in `context_injection` and prepends a structured framing block to the user message:

```
You are one step in a multi-step pipeline. Workflow: <name>.
Your role in this step: <step description from workflow editor>
---
The original user request — stay aligned with this throughout:
<content of /runs/{id}/original>
---
[CONTEXT: STEP-0/OUTPUT]
<content of /runs/{id}/step-0/output>
---
<current step instruction>
```

Every agent sees the original brief verbatim — no telephone effect, no coordinator-side truncation.

### Scratch Space

All agents in a pipeline share a scratch record at `/runs/{run_id}/scratch`. Three tools are auto-injected for all pipeline agents regardless of their manifest `tools:` list:

- **`scratch_read`** — read current scratch content
- **`scratch_write`** — overwrite scratch entirely (last write wins)
- **`submit_report`** — submit the agent's final output and exit the loop immediately

`submit_report` is the correct way for pipeline output steps (e.g. `report-writer`) to return content. The runner intercepts the call and returns its `content` argument directly — no further iterations run. Models reliably prefer calling a tool over emitting plain text, so this is more robust than prompting for a text response.

Scratch is for mid-run coordination flags and notes. Full step outputs live in `/runs/{run_id}/step-{n}/output` and are never overwritten.

### Permissions

Agents declare `read`/`write` path prefix lists in `manifest.yaml`. The KB client enforces these per-call. Two rules override manifest config:

- **`/runs/`** is always allowed for all agents — no manifest entry needed
- **Coordinator** has full access (`permissions=None`)

---

## Running Locally

```bash
# Install dependencies
pip install -r requirements.txt

# Start coordinator (needs a running agent-kb PostgreSQL)
KB_DSN=postgresql://user:pass@host/agent-kb \
LLM_MANAGER_URL=http://llm-manager.amer.dev \
uvicorn coordinator.main:app --port 8080 --reload

# Run an agent directly (bypasses Argo, useful for debugging)
TASK_ID=<uuid> \
MYCROFT_AGENT_TYPE=researcher \
KB_DSN=postgresql://user:pass@host/agent-kb \
LLM_MANAGER_URL=http://llm-manager.amer.dev \
python -m runtime
```

### Tests

```bash
pytest tests/
```

---

## Deploy

GitOps via ArgoCD. CI builds Docker images and creates deploy PRs to `k3s-dean-gitops`.

- UAT: auto-deployed on every push to `main`
- Prod: requires human PR approval

Images:
- `amerenda/mycroft:coordinator-{sha}` — coordinator service
- `amerenda/mycroft:agent-researcher-{sha}` — researcher/writer/extractor/web_search pods
- `amerenda/mycroft:agent-coder-{sha}` — coder pods (larger image with git tooling)

---

## TODO

- **Separate queue-wait timeout from inference timeout** (`common/llm.py` `_wait_for_job`): The current `JOB_TIMEOUT` is a single wall-clock limit covering both time spent in queue and time spent doing inference. A better model: fail fast if the job hasn't entered `running` state within N minutes (queue is broken or model won't load), but give inference itself a much longer or separate budget. `_wait_for_job` already tracks `t_running` — split on that to apply different limits to each phase.

---

---

## Agent Configuration — Source of Truth

Agent configuration lives in the **database**, managed through the **Agents UI tab**. The files under `agents/` are for local reference and initial seeding only — they are not the runtime source of truth.

### What the coordinator reads at startup

- `agents/<name>/manifest.yaml` — loaded via `trigger_router.load_manifests()` as a fallback if the agent has no DB record
- `agents/<name>/prompts.py` — **NOT read at runtime**. This file is dead code for the coordinator. It exists as a reference template for copy-pasting into the UI.

### How it actually works

```
startup: load_manifests() → reads manifest.yaml → populates trigger_router (no prompts)
DB poll: register()       → reads agent_definitions table → overwrites trigger_router entry (with prompts)
```

The DB version always overwrites the file version. Prompts only enter the system via `register()` (DB path). `trigger_router.get_prompts()` returns `""` for any agent not loaded from DB, so file-based `prompts.py` changes never reach the LLM.

### Rules

- **Edit agents via the UI, not git.** Changes to `agents/*/manifest.yaml` or `agents/*/prompts.py` have no effect while the coordinator is running with a DB record for that agent.
- **Prompts are DB-only.** Do not try to manage system prompts through gitops — there is no mechanism to push file contents into the DB automatically. Update prompts in the Agents tab.
- `agents/<name>/manifest.yaml` files are useful for: bootstrapping a new agent into a fresh DB, local reference, and documentation. Keep them in sync with the DB as a best-effort human record.

---

## Key Conventions

- All LLM calls route through `llm-manager` (never call Ollama/Anthropic directly)
- **DB always wins**: agent manifests and prompts are seeded from `agents/<name>/manifest.yaml` once (if not already in DB), then the DB is authoritative — edit agents via the UI, not git
- **`@group` tool syntax**: agent tool lists support `"@web"`, `"@files"`, etc. to reference DB-defined tool groups; the `@` prefix must be YAML-quoted (`"@web"`, not `@web`)
- Agents communicate via KB paths, never directly to each other
- Conversation history is persisted to the KB each iteration (restart safety)
- Don't hardcode "mycroft" in business logic — use generic terms (`coordinator`, `agent`, `platform`)
- **Forge runner** (`POST /api/forge/run`): alternative execution path for coder tasks — clones a repo, runs the Forge CLI with a custom agent prompt, returns a git diff; does not use Argo or the KB
