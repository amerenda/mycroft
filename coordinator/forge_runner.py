"""Forge runner — clones a repo, configures Forge, runs a prompt, captures results."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

log = logging.getLogger(__name__)

FORGE_TIMEOUT = 300  # 5 minutes
FORGE_BINARY = "/usr/local/bin/forge"


@dataclass
class ForgeResult:
    run_id: str = ""
    status: str = "pending"  # pending, running, completed, failed
    exit_code: int = -1
    git_diff: str = ""
    files_changed: list[str] = field(default_factory=list)
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = 0.0
    error: str = ""


# In-memory store for active/recent runs (coordinator is single-pod)
_runs: dict[str, ForgeResult] = {}


def get_run(run_id: str) -> Optional[ForgeResult]:
    return _runs.get(run_id)


def _get_github_token() -> str:
    """Get GitHub token for clone/push. Tries App auth first, falls back to PAT."""
    app_id = os.environ.get("GITHUB_APP_ID", "")
    inst_id = os.environ.get("GITHUB_APP_INSTALLATION_ID", "")
    pk = os.environ.get("GITHUB_APP_PRIVATE_KEY", "")

    if all([app_id, inst_id, pk]):
        try:
            import jwt as pyjwt
            import httpx
            now = int(time.time())
            token = pyjwt.encode({"iat": now - 60, "exp": now + 600, "iss": app_id}, pk, algorithm="RS256")
            resp = httpx.post(
                f"https://api.github.com/app/installations/{inst_id}/access_tokens",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
                timeout=15,
            )
            resp.raise_for_status()
            return resp.json()["token"]
        except Exception as e:
            log.warning("GitHub App auth failed, falling back to PAT: %s", e)

    return os.environ.get("GITHUB_TOKEN", "")


def _write_forge_config(work_dir: str, llm_url: str, llm_api_key: str, model: str,
                        system_prompt: str | None = None) -> None:
    """Write Forge configuration files.

    Global config (credentials, session) goes to ~/.forge/ so Forge can find them.
    Project-local config (custom agent) goes to <work_dir>/.forge/.
    """
    home_forge = os.path.expanduser("~/.forge")
    os.makedirs(home_forge, exist_ok=True)

    project_forge = os.path.join(work_dir, ".forge")
    os.makedirs(project_forge, exist_ok=True)
    os.makedirs(os.path.join(project_forge, "agents"), exist_ok=True)

    # Global credentials (~/.forge/.credentials.json)
    creds = [
        {
            "id": "openai_compatible",
            "auth_details": {"api_key": llm_api_key},
            "url_params": {"OPENAI_URL": llm_url.rstrip("/") + "/v1"},
        }
    ]
    with open(os.path.join(home_forge, ".credentials.json"), "w") as f:
        json.dump(creds, f)

    # Global config (~/.forge/.forge.toml)
    with open(os.path.join(home_forge, ".forge.toml"), "w") as f:
        f.write('"$schema" = "https://forgecode.dev/schema.json"\n')
        f.write('services_url = ""\n')
        f.write("max_tokens = 20480\n")
        f.write("max_requests_per_turn = 50\n")
        f.write("tool_timeout_secs = 120\n")
        f.write("top_p = 0.8\n")
        f.write("top_k = 30\n")
        f.write("\n[session]\n")
        f.write(f'provider_id = "openai_compatible"\n')
        f.write(f'model_id = "{model}"\n')
        f.write("\n[updates]\nauto_update = false\n")
        f.write("\n[reasoning]\nenabled = true\neffort = \"high\"\n")
        f.write("\n[http]\nread_timeout_secs = 600\n")

    # Project-local custom agent (<work_dir>/.forge/agents/mycroft-coder.md)
    agent_prompt = system_prompt or _default_coder_prompt()
    agent_md = f"""---
id: mycroft-coder
title: Mycroft Coder
model: {model}
tools:
  - read
  - write
  - patch
  - shell
  - fs_search
  - fetch
max_turns: 20
---

{agent_prompt}
"""
    with open(os.path.join(project_forge, "agents", "mycroft-coder.md"), "w") as f:
        f.write(agent_md)


def _default_coder_prompt() -> str:
    return """You are a senior software engineer. You receive an instruction and implement it autonomously. There is no human in the loop — you must complete the task without asking questions.

RULES:
- NEVER ask clarifying questions or request user input. You are fully autonomous.
- If something is ambiguous, read the existing code to find the answer. Look at patterns, conventions, types, imports, and neighboring code to infer intent.
- If multiple approaches are reasonable, pick the simplest one that matches existing patterns.
- Read the relevant files FIRST to understand the current code before making changes.
- Use the `read` tool to examine files, `fs_search` to find files, then `patch` or `write` to modify them.
- If you need to create a new file, use the `write` tool.
- If you need to modify an existing file, prefer `patch` over `write`.
- After making changes, read the modified files to verify correctness.
- Use `shell` to run tests or linters if they exist in the project.
- Do NOT explain what you plan to do. Act immediately.
- When done, state what you changed in one sentence."""


async def run_forge(
    instruction: str,
    repo: str,
    model: str = "qwen3:14b",
    system_prompt: str | None = None,
    llm_url: str = "",
    llm_api_key: str = "",
) -> str:
    """Run a Forge task. Returns run_id for polling."""
    run_id = str(uuid.uuid4())[:12]
    result = ForgeResult(run_id=run_id, status="running")
    _runs[run_id] = result

    asyncio.create_task(_run_forge_async(result, instruction, repo, model,
                                         system_prompt, llm_url, llm_api_key))
    return run_id


async def _run_forge_async(
    result: ForgeResult,
    instruction: str,
    repo: str,
    model: str,
    system_prompt: str | None,
    llm_url: str,
    llm_api_key: str,
) -> None:
    """Execute Forge in a temp directory (async wrapper around sync subprocess)."""
    work_dir = tempfile.mkdtemp(prefix="forge-")
    t_start = time.monotonic()

    try:
        token = _get_github_token()
        if not token:
            result.status = "failed"
            result.error = "No GitHub token available for clone"
            return

        # Clone the repo
        repo_url = repo if "://" in repo else f"https://github.com/{repo}.git"
        auth_url = repo_url.replace("https://", f"https://x-access-token:{token}@")

        log.info("Forge run %s: cloning %s", result.run_id, repo)
        clone_proc = await asyncio.create_subprocess_exec(
            "git", "clone", "--depth=1", auth_url, work_dir + "/repo",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, clone_err = await asyncio.wait_for(clone_proc.communicate(), timeout=60)
        if clone_proc.returncode != 0:
            result.status = "failed"
            result.error = f"Clone failed: {clone_err.decode()[:500]}"
            return

        repo_dir = os.path.join(work_dir, "repo")

        # Configure git identity
        subprocess.run(["git", "config", "user.name", "mycroft-forge[bot]"], cwd=repo_dir, check=False)
        subprocess.run(["git", "config", "user.email", "mycroft@amerenda.com"], cwd=repo_dir, check=False)
        subprocess.run(["git", "config", "credential.helper",
                        f"!f() {{ echo username=x-access-token; echo password={token}; }}; f"],
                       cwd=repo_dir, check=False)

        # Write Forge config
        _write_forge_config(repo_dir, llm_url, llm_api_key, model, system_prompt)

        # Run Forge
        log.info("Forge run %s: executing prompt (model=%s)", result.run_id, model)
        env = {**os.environ, "FORGE_TRACKER": "false", "HOME": "/home/mycroft"}
        proc = await asyncio.create_subprocess_exec(
            FORGE_BINARY, "--agent", "mycroft-coder",
            "-C", repo_dir,
            "-p", instruction,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=FORGE_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            result.status = "failed"
            result.error = f"Forge timed out after {FORGE_TIMEOUT}s"
            return

        result.exit_code = proc.returncode
        result.stdout = stdout.decode(errors="replace")[-5000:]
        result.stderr = stderr.decode(errors="replace")[-2000:]

        # Capture git diff (staged + unstaged combined)
        diff_proc = await asyncio.create_subprocess_exec(
            "git", "diff", "HEAD",
            cwd=repo_dir,
            stdout=asyncio.subprocess.PIPE,
        )
        diff_out, _ = await diff_proc.communicate()
        staged_diff = diff_out.decode(errors="replace")

        # Also capture unstaged changes (shell/sed modifications)
        diff_proc2 = await asyncio.create_subprocess_exec(
            "git", "diff",
            cwd=repo_dir,
            stdout=asyncio.subprocess.PIPE,
        )
        diff_out2, _ = await diff_proc2.communicate()
        unstaged_diff = diff_out2.decode(errors="replace")

        result.git_diff = (staged_diff or unstaged_diff)[:10000]

        status_proc = await asyncio.create_subprocess_exec(
            "git", "status", "--porcelain",
            cwd=repo_dir,
            stdout=asyncio.subprocess.PIPE,
        )
        status_out, _ = await status_proc.communicate()
        result.files_changed = [
            line[3:] for line in status_out.decode().strip().splitlines() if line.strip()
        ]

        result.status = "completed" if proc.returncode == 0 else "failed"
        if proc.returncode != 0 and not result.error:
            result.error = f"Forge exited with code {proc.returncode}"

        log.info("Forge run %s: %s (exit=%d, files=%d, diff=%d bytes)",
                 result.run_id, result.status, result.exit_code,
                 len(result.files_changed), len(result.git_diff))

    except Exception as e:
        log.exception("Forge run %s failed", result.run_id)
        result.status = "failed"
        result.error = str(e)
    finally:
        result.duration_seconds = time.monotonic() - t_start
        # Cleanup temp dir
        try:
            shutil.rmtree(work_dir, ignore_errors=True)
        except Exception:
            pass
