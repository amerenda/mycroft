# Mycroft — AI Agent Platform

Working name. Internal code uses generic terms (coordinator, agent, platform) for easy renaming.

## Repo Structure

| Directory | Purpose |
|-----------|---------|
| `common/` | Shared libraries: KB client, LLM client, config, models |
| `coordinator/` | FastAPI service: Telegram bot, intent classification, Argo submission |
| `runtime/` | Thin agent loop + tools (runs in ephemeral Argo Workflow pods) |
| `agents/` | Agent definitions: manifest.yaml + prompts.py per agent type |
| `workflows/` | Argo WorkflowTemplate YAMLs (applied to k3s, not used by Python) |
| `tests/` | pytest tests |

## Running Locally

```bash
# Coordinator
pip install -r requirements.txt
KB_DSN=postgresql://... uvicorn coordinator.main:app --port 8080

# Agent (bare process mode)
pip install -r requirements-agent.txt
TASK_ID=... MYCROFT_AGENT_TYPE=coder KB_DSN=... python -m runtime
```

## Key Conventions

- All LLM calls go through llm-manager (never call Ollama/Anthropic directly)
- Agents communicate via KB scoped paths, never directly
- Push to draft branches early and often (git is the durable store)
- Conversation history persisted to KB each iteration (restart safety)
- Global 5-step iteration cap enforced in agent loop
- Don't hardcode "mycroft" in business logic — use generic terms

## Agent Configuration — CRITICAL

**DB/UI always wins. Never edit prompts via git.**

- `agents/<name>/manifest.yaml` — reference/seed only. Coordinator reads this at startup ONLY if no DB record exists for that agent.
- `agents/<name>/prompts.py` — **NOT read at runtime at all**. Dead code for the coordinator. Use it as a reference template to copy into the UI.
- To change an agent's system prompt: edit it in the Agents tab of the web UI.
- To change an agent's manifest fields: edit via UI. If you also update the yaml file, it stays as a reference but has zero effect on a running coordinator.
- There is no gitops→DB sync for prompts. File edits to prompts.py never reach the LLM.

## Deploy

GitOps via ArgoCD. CI builds images and creates deploy PRs to k3s-dean-gitops.
See `plans/agent-framework/infrastructure/gitops-deploy.md` for details.
