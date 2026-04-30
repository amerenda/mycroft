# Mycroft Agent Catalog

## Source of truth

**Runtime configuration lives in the database**, edited through the **Agents** and **Workflows** tabs in the web UI. The coordinator loads DB-defined agents at startup (and on updates); files under `agents/` are **fallback seeds** only when no DB row exists for that agent name.

`agents/<name>/prompts.py` is **not** read by the coordinator. Treat it as a reference template to copy into the UI.

---

## Seed manifests (on-disk fallback)

These directories ship a `manifest.yaml` for cold starts or dev without DB seed data:

| Directory   | `name` (manifest) | Role |
|------------|-------------------|------|
| `_coder/`  | `coder`           | Code changes, PRs |
| `_writer/` | `writer`          | Report synthesis |
| `_extractor/` | `extractor`    | Structured extraction |
| `playground/` | `playground`   | Sandboxed experimentation |
| `web_search/` | `web-search`   | Lightweight search sub-agent |
| `report_writer/` | `report-writer` | Final report formatting |

Pipeline workflows and extra agent types (including names like `researcher`) are created in the **Workflows** / **Agents** UI, not by adding new folders here.

---

## Planned agents (product direction)

### Planner

Interactive planning: chat with the user to produce an implementation plan for the coder. Human-in-the-loop. Triggers: API, UI.

### Reviewer

Adversarial code review: diff, tests, edge cases; can hand back to coder. Triggers: API, automation hooks.

### Documenter

Keep READMEs and API docs current from code changes. Triggers: schedule, post-merge.

### QA agent

End-to-end checks in UAT. Triggers: pipeline step, API.

---

## Prompt engineering (reference)

Techniques that work well across agents (personas, constraints, good/bad examples, phased protocols, etc.) are described in the older design notes in git history and in `README.md`. Prefer editing prompts in the **Agents** UI so all environments stay aligned.

---

## Architecture notes

- All agent types share the same runtime (`runtime/runner.py`) and tool registry.
- **Directory naming:** use underscores (`web_search/`). Manifest `name` may use hyphens (`web-search`).
- Argo `WorkflowTemplate` references per environment live in GitOps (`k3s-dean-gitops`); image tags are unified (`agent`, `agent-coder-*`, `agent-researcher-*` aliases on the same runtime image).
- **`/api/tasks`** accepts any `agent_type` the coordinator knows (DB-registered or seed manifest).
