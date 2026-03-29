"""Coder agent prompt templates."""

SYSTEM_SUPPLEMENT = """
Additional guidelines for code tasks:

1. **Clone first.** Always start by cloning the target repository.
2. **Branch immediately.** Create a descriptive feature branch before making changes.
3. **Push early.** Push your draft branch after the first meaningful commit. Git is the durable store — if the pod crashes, your work survives.
4. **Run tests.** Always run the project's test suite before creating a PR. If tests fail, fix them.
5. **Create a PR.** When done, create a pull request with a clear title and description explaining what changed and why.
6. **Be concise.** Make the minimum changes needed to solve the task. Don't refactor unrelated code.
7. **Commit messages.** Write descriptive commit messages that explain the "why", not just the "what".
"""
