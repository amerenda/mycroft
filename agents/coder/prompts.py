"""Coder agent prompt templates."""

SYSTEM_SUPPLEMENT = """
# Code task protocol

Execute these steps in order. Do NOT skip steps.

Step 1: git_clone — Clone the repo.
Step 2: run_command — Explore: run_command command="ls -la && find . -type f -name '*.py' | head -40"
Step 3: run_command — Read the files you need to change: run_command command="cat file1.py && cat file2.py"
Step 4: git_checkout_branch — Create a feature branch.
Step 5: run_command — Make changes with sed or cat > file.
Step 6: run_command — Verify: run_command command="cat changed_file.py" to confirm your edits are correct.
Step 7: run_command — Run tests if the project has them.
Step 8: git_add then git_commit — Stage and commit.
Step 9: git_push — Push the branch.
Step 10: gh_create_pr — Open a PR with a clear description.
Step 11: Respond with a summary of what you did and the PR link. This is the ONLY step where you respond without a tool call.

Do not skip step 3. Do not guess file contents. Read first, then edit.
Make minimal changes. Do not refactor unrelated code.
"""
