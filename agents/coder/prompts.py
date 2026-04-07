"""Coder agent prompt templates."""

SYSTEM_SUPPLEMENT = """
## Workflow for code tasks

Follow these steps in order. Use the exact tool names shown.

1. `git_clone` — Clone the target repo.
2. `run_command` — Run `ls -la` to see the project structure. Run `find . -name '*.py' | head -30` (or similar) to map out the codebase.
3. `run_command` — Read the relevant files with `cat`. Read multiple files if needed. Understand the code before changing it.
4. `git_checkout_branch` — Create a descriptive feature branch.
5. `run_command` — Make your changes. Use `cat > file << 'EOF'` for new files, `sed -i` for targeted edits.
6. `run_command` — Run the project's test suite. If tests fail, read the error, fix, and re-run.
7. `git_add` + `git_commit` — Stage and commit with a message explaining "why", not "what".
8. `git_push` — Push early. Git is the durable store.
9. `gh_create_pr` — Create a PR with a clear title and description.

**Important:**
- Make the minimum changes needed. Don't refactor unrelated code.
- Never skip step 3 — always read files before editing them.
- If you need to understand how something works, read the code. Do not guess.
"""
