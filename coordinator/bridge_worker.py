"""Bridge worker — long-lived pod that executes shell commands on behalf of the coordinator."""

from __future__ import annotations

import asyncio
import logging
import os

from fastapi import FastAPI
from pydantic import BaseModel

log = logging.getLogger(__name__)

WORKSPACE = os.environ.get("WORKSPACE", "/workspace")

app = FastAPI()


class RunRequest(BaseModel):
    command: str
    cwd: str = ""


@app.post("/run")
async def run(req: RunRequest):
    if req.cwd:
        cwd = req.cwd if req.cwd.startswith("/") else os.path.join(WORKSPACE, req.cwd)
    else:
        cwd = WORKSPACE

    os.makedirs(cwd, exist_ok=True)
    log.info("run: %s (cwd=%s)", req.command, cwd)

    try:
        proc = await asyncio.create_subprocess_shell(
            req.command,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
    except asyncio.TimeoutError:
        return {"output": "Error: command timed out after 120 seconds", "exit_code": -1}

    output = ""
    if stdout:
        output += stdout.decode(errors="replace")
    if stderr:
        output += "\nSTDERR:\n" + stderr.decode(errors="replace")
    if proc.returncode != 0:
        output = f"Exit code {proc.returncode}\n{output}"
    if len(output) > 10000:
        output = output[:5000] + "\n... (truncated) ...\n" + output[-3000:]

    return {"output": output.strip() or "(no output)", "exit_code": proc.returncode}


@app.get("/health")
async def health():
    return {"status": "ok"}
