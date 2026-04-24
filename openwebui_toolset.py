"""
Mycroft Tool Bridge — Open WebUI Toolset

Install in Open WebUI: Workspace → Tools → New Tool → paste this file.

Configure MYCROFT_URL to your coordinator's public URL before saving.
"""

import json
import urllib.request
import urllib.error
from typing import Any

MYCROFT_URL = "https://mycroft.amer.dev"


def _call_bridge(tool: str, args: dict) -> str:
    """POST to Mycroft bridge and return the result string."""
    payload = json.dumps({"tool": tool, "args": args}).encode()
    req = urllib.request.Request(
        f"{MYCROFT_URL}/api/bridge/run-tool",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read())
            return body.get("result", "")
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        return f"Error {e.code}: {detail}"
    except Exception as e:
        return f"Error: {e}"


def _call_coordinator(path: str, method: str = "GET", payload: dict | None = None) -> Any:
    """Call Mycroft coordinator API. Returns parsed JSON."""
    url = f"{MYCROFT_URL}{path}"
    data = json.dumps(payload).encode() if payload else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"} if data else {},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        raise RuntimeError(f"HTTP {e.code}: {detail}")


class Tools:
    def __init__(self):
        pass

    def kb_search(self, query: str, scopes: str = "/", limit: int = 5) -> str:
        """
        Search the Mycroft knowledge base using semantic vector search.

        :param query: The search query.
        :param scopes: Comma-separated KB path prefixes to search (e.g. "/agents/researcher,/tasks"). Defaults to root.
        :param limit: Maximum number of results to return (1-20).
        :return: Matching KB records with their scope paths.
        """
        scope_list = [s.strip() for s in scopes.split(",") if s.strip()]
        if not scope_list:
            scope_list = ["/"]
        return _call_bridge("kb_search", {
            "query": query,
            "scopes": scope_list,
            "limit": min(max(1, limit), 20),
        })

    def web_search(self, query: str) -> str:
        """
        Search the web using the Mycroft SearXNG instance.

        :param query: The search query.
        :return: Web search results as formatted text.
        """
        return _call_bridge("web_search", {"query": query})

    def web_read(self, url: str, extract: str = "") -> str:
        """
        Fetch and read a web page. Optionally extract specific information.

        :param url: The URL to fetch.
        :param extract: Optional instruction for what to extract from the page (e.g. "main article text").
        :return: Page content or extracted information.
        """
        args: dict[str, Any] = {"url": url}
        if extract:
            args["extract"] = extract
        return _call_bridge("web_read", args)

    def run_command(self, command: str, cwd: str = "") -> str:
        """
        Run a shell command on the Mycroft coordinator host (murderbot).
        Use for quick diagnostics, file inspection, or running scripts.

        :param command: The shell command to run.
        :param cwd: Optional working directory (relative to /tmp/bridge-workspace).
        :return: Command stdout/stderr output.
        """
        args: dict[str, Any] = {"command": command}
        if cwd:
            args["cwd"] = cwd
        return _call_bridge("run_command", args)

    def start_task(self, agent_type: str, prompt: str, model: str = "") -> str:
        """
        Start a long-running Mycroft agent task (researcher, coder, etc.) asynchronously.
        Returns a task_id — use get_task() to poll for completion.

        :param agent_type: Agent type to run (e.g. "researcher", "coder").
        :param prompt: The task description or question.
        :param model: Optional model override (leave blank for agent default).
        :return: JSON with task_id to use with get_task().
        """
        payload: dict[str, Any] = {
            "agent_type": agent_type,
            "instruction": prompt,
            "trigger": "chat",
        }
        if model:
            payload["model"] = model
        try:
            result = _call_coordinator("/api/tasks", method="POST", payload=payload)
            task_id = result.get("task_id") or result.get("id", "")
            return json.dumps({"task_id": task_id, "status": "pending",
                               "message": f"Task started. Poll with get_task('{task_id}')"})
        except RuntimeError as e:
            return f"Error starting task: {e}"

    def get_task(self, task_id: str) -> str:
        """
        Get the status and result of a Mycroft agent task.

        :param task_id: The task ID returned by start_task().
        :return: JSON with status (pending/running/completed/failed) and result if done.
        """
        try:
            task = _call_coordinator(f"/api/tasks/{task_id}")
            out: dict[str, Any] = {
                "task_id": task_id,
                "status": task.get("status"),
                "agent_type": task.get("agent_type"),
            }
            if task.get("result"):
                result = task["result"]
                out["summary"] = result.get("summary", "")
                out["kb_scope"] = result.get("kb_scope", "")
            if task.get("status") not in ("completed", "failed"):
                out["message"] = "Task still running. Call get_task() again in a few seconds."
            return json.dumps(out, indent=2)
        except RuntimeError as e:
            return f"Error fetching task: {e}"
