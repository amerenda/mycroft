# Mycroft

AI agent platform running on k3s. Accepts tasks via Telegram or the web UI, runs agents as ephemeral Argo Workflow pods, stores knowledge in pgvector, and produces reports.

---

## Architecture

```
Browser / Telegram
       │
       ▼
  Coordinator (FastAPI)
  ├── Task Manager  ─────────────────► PostgreSQL (agent-kb)
  ├── Argo Submitter ────────────────► k3s / Argo Workflows
  ├── Telegram Bot
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
| `coordinator/` | FastAPI service: task API, Telegram bot, Argo submission, report storage |
| `runtime/` | Thin agent loop that runs inside Argo Workflow pods |
| `agents/` | Agent definitions: `manifest.yaml` + `prompts.py` per agent type |
| `common/` | Shared libraries: KB client, LLM client, config, models |
| `frontend/` | Single-page web UI |
| `workflows/` | Argo WorkflowTemplate YAMLs (for legacy template-based agents) |

### Agents

| Agent | Purpose | Trigger |
|-------|---------|---------|
| `researcher` | Web research → structured report | Telegram "research" intent, API, UI |
| `coder` | Clone repo, implement changes, open PR | Telegram "engineering" intent, API, UI |
| `writer` | Turn gathered findings into a report | Pipeline phase 2 (research-regular/deep) |
| `extractor` | Extract structured data from text | Pipeline, API |
| `web_search` | Lightweight web search sub-agent | Pipeline step |

### Workflows (Pipelines)

| Workflow | Description |
|----------|-------------|
| `research-quick` | Single researcher agent, ~2 min |
| `research-regular` | Gather agent → Writer agent, ~8 min |
| `research-deep` | Extended gather + write, ~15 min |
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

All agents in a pipeline share a scratch record at `/runs/{run_id}/scratch`. Two tools are auto-injected for pipeline agents:

- **`scratch_read`** — read current scratch content
- **`scratch_write`** — overwrite scratch entirely (last write wins)

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

## Key Conventions

- All LLM calls route through `llm-manager` (never call Ollama/Anthropic directly)
- Agents communicate via KB paths, never directly to each other
- Conversation history is persisted to the KB each iteration (restart safety)
- Agent manifests live in `agents/<name>/manifest.yaml` and are also stored in the DB for UI-created agents
- Don't hardcode "mycroft" in business logic — use generic terms (`coordinator`, `agent`, `platform`)
