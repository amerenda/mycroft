# Agent catalog

Legacy in-repo manifests (`coder`, `researcher`, `extractor`) were removed. Define agents again under `agents/<name>/` with `manifest.yaml` and optional `prompts.py`, or create them from the **Agents** tab in Agent Studio (writes the same paths).

Shared runtime: `runtime/runner.py`. Tools are declared in the manifest and resolved in `runtime/tools/base.py`.

Trigger rules (`triggers` in `manifest.yaml`) are loaded by `coordinator/trigger_router.py` at coordinator startup.

---

## Prompt engineering (reference)

Techniques worth reusing when authoring `prompts.py` supplements:

1. **Persona-driven identity** — establish a decision lens, not only a job title.
2. **Constraint emphasis** — lead with `CRITICAL:`, use clear prohibitions where it matters.
3. **Good/bad examples inline** — show efficient vs wasteful tool use.
4. **Meta-cognitive guidance** — name failure modes and corrections.
5. **Output format specification** — exact templates, field names, structure.
6. **Adversarial mindset** (reviewers) — require evidence, not opinion.
7. **Efficiency** — combine commands, batch reads, parallel fetches where safe.
8. **Context briefing** — brief downstream steps like a colleague walking in.
9. **Strength/weakness awareness** — when to defer vs act.
10. **Phase-based protocols** — named phases (e.g. understand → implement → ship).

(Source: patterns discussed for this platform; OpenClaude-style ideas were cited in older drafts.)

---

## Example agent types (not shipped in-repo)

Example roles: **coder** (repo + PR), **web-search** (gather sources), **extractor** (URLs → facts). Each is an `agents/<name>/` folder plus an Argo `WorkflowTemplate` named `agent-<name>` in GitOps when deploying.
