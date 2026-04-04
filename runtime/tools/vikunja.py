"""Vikunja todo tools for agent runtime."""

from __future__ import annotations

import os
from typing import Any

import httpx


def _client() -> httpx.AsyncClient:
    token = os.environ["VIKUNJA_TOKEN"]
    base_url = os.environ.get("VIKUNJA_URL", "https://todo.amer.dev")
    return httpx.AsyncClient(
        base_url=f"{base_url}/api/v1",
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )


class TodoListProjects:
    name = "todo_list_projects"
    description = "List all Vikunja projects with their IDs."
    parameters: dict[str, Any] = {"type": "object", "properties": {}, "required": []}

    async def execute(self, args: dict[str, Any]) -> str:
        async with _client() as c:
            r = await c.get("/projects")
            r.raise_for_status()
        projects = r.json()
        lines = [f"{p['id']}: {p['title']}" for p in projects]
        return "\n".join(lines) if lines else "No projects found."


class TodoGetTasks:
    name = "todo_get_tasks"
    description = "Get tasks from a Vikunja project. Returns task IDs, titles, and done status."
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "project_id": {"type": "integer", "description": "Project ID to fetch tasks from"},
            "include_done": {"type": "boolean", "description": "Include completed tasks (default false)"},
        },
        "required": ["project_id"],
    }

    async def execute(self, args: dict[str, Any]) -> str:
        project_id = args["project_id"]
        include_done = args.get("include_done", False)
        async with _client() as c:
            r = await c.get(f"/projects/{project_id}/tasks", params={"per_page": 100})
            r.raise_for_status()
        tasks = r.json()
        if not include_done:
            tasks = [t for t in tasks if not t.get("done")]
        if not tasks:
            return "No tasks found."
        lines = [f"{t['id']} [{'done' if t['done'] else 'open'}] {t['title']}" for t in tasks]
        return "\n".join(lines)


class TodoCreateTask:
    name = "todo_create_task"
    description = "Create a new task in a Vikunja project."
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "project_id": {"type": "integer", "description": "Project ID to create the task in"},
            "title": {"type": "string", "description": "Task title"},
            "description": {"type": "string", "description": "Optional task description (markdown)"},
        },
        "required": ["project_id", "title"],
    }

    async def execute(self, args: dict[str, Any]) -> str:
        project_id = args["project_id"]
        payload: dict[str, Any] = {"title": args["title"]}
        if "description" in args:
            payload["description"] = args["description"]
        async with _client() as c:
            r = await c.put(f"/projects/{project_id}/tasks", json=payload)
            r.raise_for_status()
        task = r.json()
        return f"Created task {task['id']}: {task['title']}"


class TodoUpdateTask:
    name = "todo_update_task"
    description = "Update a Vikunja task — mark done, change title, or update description."
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "task_id": {"type": "integer", "description": "Task ID to update"},
            "done": {"type": "boolean", "description": "Mark task as done or not done"},
            "title": {"type": "string", "description": "New title"},
            "description": {"type": "string", "description": "New description (markdown)"},
        },
        "required": ["task_id"],
    }

    async def execute(self, args: dict[str, Any]) -> str:
        task_id = args["task_id"]
        payload = {k: v for k, v in args.items() if k != "task_id"}
        async with _client() as c:
            r = await c.post(f"/tasks/{task_id}", json=payload)
            r.raise_for_status()
        task = r.json()
        status = "done" if task.get("done") else "open"
        return f"Updated task {task['id']}: {task['title']} [{status}]"
