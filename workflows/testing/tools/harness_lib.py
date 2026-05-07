"""Shared HTTP and polling helpers for workflows/testing E2E scripts.

Keep coordinator/runtime generic; this module is test-harness only.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from typing import Any


def http_json(
    method: str,
    base: str,
    path: str,
    payload: dict | None = None,
    *,
    timeout: float = 120.0,
    bearer_token: str = "",
) -> Any:
    url = base + path
    data = None
    headers: dict[str, str] = {}
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode("utf-8")
    return json.loads(body) if body else None


def fetch_models(llm_manager_base: str, timeout: float = 60.0) -> list[dict[str, Any]]:
    raw = http_json("GET", llm_manager_base, "/api/models", None, timeout=timeout)
    if not isinstance(raw, list):
        raise RuntimeError(f"Unexpected /api/models shape: {type(raw).__name__}")
    return raw


def param_sort_key(m: dict[str, Any]) -> float:
    pc = m.get("parameter_count")
    if isinstance(pc, (int, float)):
        return float(pc)
    if isinstance(pc, str):
        digits = re.sub(r"[^0-9.]", "", pc.split()[0] if pc else "")
        try:
            return float(digits) if digits else 0.0
        except ValueError:
            return 0.0
    return float(m.get("vram_estimate_gb") or 0)


def select_new_models(
    models: list[dict[str, Any]],
    max_models: int,
    *,
    baseline_models: frozenset[str] | set[str],
) -> list[str]:
    """Pick diverse chat models that fit a runner, excluding baseline names."""
    rows: list[dict[str, Any]] = []
    for m in models:
        if m.get("is_alias"):
            continue
        name = (m.get("name") or "").strip()
        if not name or name in baseline_models:
            continue
        if m.get("downloaded") is False:
            continue
        if not m.get("fits", True):
            continue
        rows.append(m)

    if not rows:
        for m in models:
            if m.get("is_alias"):
                continue
            name = (m.get("name") or "").strip()
            if name and name not in baseline_models:
                rows.append(m)

    rows.sort(key=lambda m: (param_sort_key(m), m.get("name") or ""))
    if not rows:
        return []

    n = len(rows)
    idxs = sorted({0, n // 4, (2 * n) // 3, n - 1}) if n > 1 else [0]
    picked: list[str] = []
    for i in idxs:
        nm = rows[i].get("name")
        if nm and nm not in picked:
            picked.append(nm)
        if len(picked) >= max_models:
            break
    for m in rows:
        if len(picked) >= max_models:
            break
        nm = m.get("name")
        if nm and nm not in picked:
            picked.append(nm)
    return picked[:max_models]


def wf_slug_prefix(prefix: str, model: str, prompt_hash: str, hash_len: int = 6) -> str:
    """Workflow clone name: prefix + first hash_len chars of prompt_hash + sanitized model."""
    safe_model = re.sub(r"[^a-z0-9-]+", "-", model.lower()).strip("-")[:22]
    return f"{prefix}{prompt_hash[:hash_len]}-{safe_model}"


def wait_run_id(
    mycroft_base: str,
    first_task_id: str,
    *,
    bearer_token: str = "",
    run_id_wait_sec: float = 180.0,
    poll_sec: float = 2.0,
) -> str | None:
    t0 = time.time()
    while time.time() - t0 < run_id_wait_sec:
        try:
            td = http_json(
                "GET",
                mycroft_base,
                f"/api/tasks/{first_task_id}",
                None,
                timeout=60.0,
                bearer_token=bearer_token,
            )
        except urllib.error.HTTPError as e:
            if e.code in (429, 502, 503, 504):
                time.sleep(min(30.0, poll_sec * 5))
                continue
            raise
        if isinstance(td, dict):
            rid = (td.get("config") or {}).get("run_id")
            if rid:
                return str(rid)
        time.sleep(poll_sec)
    return None


def wait_until_agent_terminal(
    mycroft_base: str,
    workflow_name: str,
    run_id: str,
    agent_type: str,
    *,
    bearer_token: str = "",
    per_run_timeout_sec: float,
    poll_sec: float = 20.0,
    runs_limit: int = 200,
) -> dict[str, Any] | None:
    """Poll workflow runs until the given agent_type has completed or failed for run_id."""
    t0 = time.time()
    path = f"/api/workflows/{workflow_name}/runs?limit={runs_limit}"
    while time.time() - t0 < per_run_timeout_sec:
        try:
            rr = http_json("GET", mycroft_base, path, None, timeout=120.0, bearer_token=bearer_token)
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504):
                time.sleep(15)
                continue
            raise
        if not isinstance(rr, list):
            time.sleep(15)
            continue
        for x in rr:
            if x.get("agent_type") != agent_type:
                continue
            if (x.get("config") or {}).get("run_id") != run_id:
                continue
            st = x.get("status")
            if st in ("completed", "failed"):
                return x
        time.sleep(poll_sec)
    return None


def put_workflow(
    mycroft_base: str,
    name: str,
    *,
    content: str,
    pipeline_json: dict[str, Any],
    bearer_token: str = "",
) -> None:
    http_json(
        "PUT",
        mycroft_base,
        f"/api/workflows/{name}",
        {"content": content, "pipeline_json": pipeline_json},
        timeout=120.0,
        bearer_token=bearer_token,
    )


def post_task_workflow(
    mycroft_base: str,
    workflow: str,
    instruction: str,
    *,
    bearer_token: str = "",
) -> Any:
    return http_json(
        "POST",
        mycroft_base,
        "/api/tasks",
        {"workflow": workflow, "instruction": instruction},
        timeout=120.0,
        bearer_token=bearer_token,
    )


def testing_output_dir(tools_dir: str, workflow_folder: str, today: str) -> str:
    """Resolved path workflows/testing/<workflow_folder>/<today>/."""
    import os

    return os.path.normpath(os.path.join(tools_dir, "..", workflow_folder, today))
