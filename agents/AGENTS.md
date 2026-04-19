# Mycroft Agent Catalog

## Active Agents

### Coder
**Status:** Active
**Model:** qwen3:14b
**Purpose:** Implement code changes, open PRs
**Tools:** files, git, github, shell
**Trigger:** Telegram "engineering" intent, API

Autonomous coding agent. Clones repo, reads code, makes changes, runs tests, opens PR. No human input during execution.

### Researcher
**Status:** Active
**Model:** qwen3:14b
**Purpose:** Investigate topics, produce actionable reports
**Tools:** files, shell
**Trigger:** Telegram "research" intent, API

Fetches information from multiple sources via curl, synthesizes findings into a structured report at /workspace/report.md. Reports are sent to Telegram.

---

## Planned Agents

### Planner
**Status:** Planned (Phase 2)
**Purpose:** Interactive planning — chat with user to construct implementation plan
**Model:** TBD (needs good conversation + reasoning)
**Tools:** files, shell (read-only)
**Trigger:** Telegram, API, UI chat

Unlike coder/researcher, this agent takes user input. It asks questions, proposes approaches, and produces a plan document that the coder agent can execute. Human-in-the-loop.

### Reviewer
**Status:** Planned (Phase 2)
**Purpose:** Review PRs, test changes in UAT
**Model:** TBD
**Tools:** files, git, shell (read + run tests)
**Trigger:** PR created event, API

Adversarial code reviewer. "Your job is not to confirm it works — it's to try to break it." Reads the diff, runs tests, checks edge cases. Can return to coder if issues found.

Key prompt technique from OpenClaude: require structured output with PASS/FAIL verdicts and specific evidence. Include adversarial probes: concurrency, boundary values, idempotency.

### Documenter
**Status:** Planned (Phase 3)
**Purpose:** Keep READMEs, CLAUDE.md, API docs current
**Model:** TBD (smaller model fine)
**Tools:** files, git, github, shell
**Trigger:** Scheduled, post-merge hook

Runs periodically or after merges. Reads code changes, updates documentation. Self-maintaining.

### QA Agent
**Status:** Planned (Phase 3)
**Purpose:** End-to-end testing in UAT environment
**Model:** TBD
**Tools:** shell (httpx/curl for API testing), files
**Trigger:** After reviewer approves, before human review

Hits UAT endpoints, runs integration tests, verifies the deployment works. Reports pass/fail to Telegram.

---

## Prompt Engineering Techniques

### From OpenClaude (Apache-2.0, github.com/Gitlawb/openclaude)

1. **Persona-driven identity** — "You are a X specialist" establishes decision-making framework. Not just a role description but a lens for all decisions.

2. **Constraint emphasis** — Lead with "CRITICAL:", use "STRICTLY PROHIBITED", repetition. The model pays attention to formatting weight.

3. **Good/bad examples inline** — Don't just say "be efficient". Show:
   ```
   GOOD: run_command command="cat a.py && cat b.py"
   BAD: run_command command="cat a.py"  (wait) run_command command="cat b.py"
   ```

4. **Meta-cognitive guidance** — Tell the model to watch for its own failure patterns:
   - "Recognize your own rationalizations"
   - "If you catch yourself [pattern], do [correction]"
   - List specific anti-patterns by name (scope creep, confirmation bias, etc.)

5. **Output format specification** — Exact templates, not vague "write a report." Show the structure, field names, what goes where.

6. **Adversarial mindset** (for reviewers) — "Your job is not to confirm it works — it's to try to break it." Require evidence, not opinion.

7. **Efficiency through parallelism** — "Combine multiple commands with && to save iterations." "Fetch multiple URLs in one run_command call."

8. **Context briefing pattern** (for agent spawning) — "Brief the agent like a smart colleague who just walked in. Explain what you're trying to accomplish and WHY, what's been learned or ruled out."

9. **Strength/weakness awareness** — Enumerate what the agent is good at, so it knows when to defer vs when to act confidently.

10. **Phase-based protocols** — Break work into named phases (Understand → Implement → Ship). The model follows sequential structure better than free-form instructions.

---

## Agent Architecture Notes

- All agents share the same runtime (`runtime/runner.py`) and tool registry
- Agent identity comes from `manifest.yaml` (model, tools, permissions) + `prompts.py` (system supplement)
- Adding a new agent = create `agents/{name}/manifest.yaml` + optional `prompts.py`
- Argo WorkflowTemplate per agent in k3s-dean-gitops
- All agents use the same Docker image (`agent-coder`) — the manifest determines behavior
- Intent classification routes Telegram messages to the right agent
- The coordinator's `/api/tasks` endpoint accepts any agent_type
