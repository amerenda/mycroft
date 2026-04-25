"""Submit Argo Workflows and monitor their progress."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, Coroutine

log = logging.getLogger(__name__)

# Workflow is stale if no phase change in this many seconds
STALE_TIMEOUT = 300  # 5 minutes

# Agent type → Docker image target. Agents not listed use the researcher image,
# which includes all agents/ code and web tools.
_AGENT_IMAGE: dict[str, str] = {
    "coder": "agent-coder",
}
_DEFAULT_IMAGE = "agent-researcher"


class ArgoSubmitter:
    """Submits Argo Workflow CRDs and monitors progress."""

    def __init__(
        self,
        namespace: str = "mycroft",
        image_repo: str = "amerenda/mycroft",
        image_tag: str = "latest",
        llm_manager_url: str = "http://llm-manager-backend.llm-manager.svc:8081",
        credentials_secret: str = "mycroft-credentials",
    ):
        self.namespace = namespace
        self.image_repo = image_repo
        self.image_tag = image_tag
        self.llm_manager_url = llm_manager_url
        self.credentials_secret = credentials_secret
        self._api = None
        self._watchers: dict[str, asyncio.Task] = {}   # wf_name → watcher Task
        self._task_to_wf: dict[str, str] = {}           # task_id → wf_name
        self._wf_to_task: dict[str, str] = {}           # wf_name → task_id

    def _get_api(self):
        if self._api is None:
            from kubernetes import client, config
            try:
                config.load_incluster_config()
            except config.ConfigException:
                config.load_kube_config()
            self._api = client.CustomObjectsApi()
        return self._api

    def _build_workflow(
        self,
        agent_type: str,
        task_id: str,
        params: dict[str, Any] | None = None,
        manifest: Any | None = None,
    ) -> dict:
        """Build an inline Argo Workflow spec from the agent manifest.

        If manifest is None and a WorkflowTemplate exists for this agent type,
        falls back to workflowTemplateRef so pre-existing templates still work.
        When manifest is provided, always generates inline — no template required.
        """
        p = params or {}
        arguments = {
            "parameters": [
                {"name": "task-id", "value": task_id},
                {"name": "config", "value": json.dumps(p)},
                {"name": "model-override", "value": p.get("model_override", "")},
            ]
        }

        if manifest is None:
            # Legacy path: reference a pre-created WorkflowTemplate
            return {
                "apiVersion": "argoproj.io/v1alpha1",
                "kind": "Workflow",
                "metadata": {
                    "generateName": f"{agent_type}-{task_id[:8]}-",
                    "namespace": self.namespace,
                },
                "spec": {
                    "workflowTemplateRef": {"name": f"agent-{agent_type}"},
                    "arguments": arguments,
                },
            }

        # Inline spec — no pre-created template needed
        image_key = _AGENT_IMAGE.get(agent_type, _DEFAULT_IMAGE)
        image = f"{self.image_repo}:{image_key}-{self.image_tag}"

        res = manifest.resources
        template_name = f"run-{agent_type}"

        return {
            "apiVersion": "argoproj.io/v1alpha1",
            "kind": "Workflow",
            "metadata": {
                "generateName": f"{agent_type}-{task_id[:8]}-",
                "namespace": self.namespace,
            },
            "spec": {
                "entrypoint": template_name,
                "arguments": arguments,
                "templates": [{
                    "name": template_name,
                    "activeDeadlineSeconds": 3600,
                    "retryStrategy": {"limit": 3, "backoff": {"duration": "30s", "factor": 2}},
                    "container": {
                        "image": image,
                        "imagePullPolicy": "Always",
                        "env": [
                            {"name": "TASK_ID", "value": "{{workflow.parameters.task-id}}"},
                            {"name": "MYCROFT_AGENT_TYPE", "value": agent_type},
                            {"name": "KB_DSN", "valueFrom": {"secretKeyRef": {
                                "name": self.credentials_secret, "key": "kb-dsn"}}},
                            {"name": "LLM_MANAGER_URL", "value": self.llm_manager_url},
                            {"name": "LLM_REGISTRATION_SECRET", "valueFrom": {"secretKeyRef": {
                                "name": self.credentials_secret, "key": "llm-registration-secret"}}},
                            {"name": "MODEL_OVERRIDE",
                             "value": "{{workflow.parameters.model-override}}"},
                            {"name": "TRANSFORMERS_OFFLINE", "value": "1"},
                        ],
                        "resources": {
                            "limits": {
                                "memory": res.memory,
                                "cpu": res.cpu,
                                "ephemeral-storage": res.scratch,
                            },
                            "requests": {"memory": "256Mi", "cpu": "500m"},
                        },
                        "volumeMounts": [{"name": "scratch", "mountPath": "/workspace"}],
                    },
                    "volumes": [{"name": "scratch", "emptyDir": {"sizeLimit": res.scratch}}],
                }],
            },
        }

    async def submit(
        self,
        agent_type: str,
        task_id: str,
        params: dict[str, Any] | None = None,
        manifest: Any | None = None,
        on_update: Callable[[str, str, str], Coroutine] | None = None,
    ) -> str:
        """Submit an Argo Workflow. Pass manifest to generate inline spec (no WorkflowTemplate needed)."""
        workflow = self._build_workflow(agent_type, task_id, params, manifest)

        api = self._get_api()
        result = api.create_namespaced_custom_object(
            group="argoproj.io",
            version="v1alpha1",
            namespace=self.namespace,
            plural="workflows",
            body=workflow,
        )

        wf_name = result["metadata"]["name"]
        log.info("Submitted Argo Workflow: %s (agent=%s, task=%s)", wf_name, agent_type, task_id[:8])

        # Track task ↔ workflow mapping for cancellation
        self._task_to_wf[task_id] = wf_name
        self._wf_to_task[wf_name] = task_id

        # Start background watcher
        if on_update:
            self._watchers[wf_name] = asyncio.create_task(
                self._watch_workflow(wf_name, task_id, on_update)
            )

        return wf_name

    async def terminate_task(self, task_id: str) -> bool:
        """Stop the Argo Workflow for a task. Returns True if a workflow was found and stopped."""
        wf_name = self._task_to_wf.get(task_id)
        if not wf_name:
            return False
        return await self._terminate_workflow(wf_name)

    async def _terminate_workflow(self, wf_name: str) -> bool:
        """Stop a workflow by name, cancel its watcher, and clean up mappings."""
        # Cancel the in-process watcher
        watcher = self._watchers.pop(wf_name, None)
        if watcher and not watcher.done():
            watcher.cancel()

        # Clean up bidirectional mapping
        task_id = self._wf_to_task.pop(wf_name, None)
        if task_id:
            self._task_to_wf.pop(task_id, None)

        api = self._get_api()
        try:
            api.patch_namespaced_custom_object(
                group="argoproj.io",
                version="v1alpha1",
                namespace=self.namespace,
                plural="workflows",
                name=wf_name,
                body={"spec": {"shutdown": "Terminate"}},
            )
            log.info("Stopped Argo Workflow: %s", wf_name)
            return True
        except Exception as e:
            log.warning("Could not stop workflow %s (may already be done): %s", wf_name, e)
            return False

    async def _watch_workflow(
        self,
        wf_name: str,
        task_id: str,
        on_update: Callable[[str, str, str], Coroutine],
    ):
        """Poll workflow status and send updates on changes."""
        api = self._get_api()
        last_phase = None
        stale_since = 0

        try:
            while True:
                await asyncio.sleep(10)
                try:
                    wf = api.get_namespaced_custom_object(
                        group="argoproj.io",
                        version="v1alpha1",
                        namespace=self.namespace,
                        plural="workflows",
                        name=wf_name,
                    )
                except Exception:
                    log.warning("Workflow %s not found — may have been deleted", wf_name)
                    await on_update(task_id, "unknown", f"Workflow {wf_name} not found")
                    return

                status = wf.get("status", {})
                phase = status.get("phase", "Pending")
                message = status.get("message", "")

                # Send update on phase change
                if phase != last_phase:
                    stale_since = 0
                    last_phase = phase
                    log.info("Workflow %s: %s %s", wf_name, phase, message)

                    if phase in ("Succeeded", "Failed", "Error"):
                        await on_update(task_id, phase.lower(), message or f"Workflow {phase.lower()}")
                        return
                    elif phase == "Running":
                        # Don't notify on Running — that's expected
                        pass
                else:
                    stale_since += 10
                    if stale_since >= STALE_TIMEOUT and phase == "Running":
                        stale_since = 0  # reset so we don't spam
                        await on_update(task_id, "stale", f"Agent has been running for {STALE_TIMEOUT}s with no progress")

        except asyncio.CancelledError:
            return
        except Exception:
            log.exception("Error watching workflow %s", wf_name)
        finally:
            self._watchers.pop(wf_name, None)
            task_id = self._wf_to_task.pop(wf_name, None)
            if task_id:
                self._task_to_wf.pop(task_id, None)
