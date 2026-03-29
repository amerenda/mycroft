"""Submit Argo Workflows for agent execution."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

log = logging.getLogger(__name__)


class ArgoSubmitter:
    """Submits Argo Workflow CRDs for agent tasks."""

    def __init__(self, namespace: str = "mycroft", image_repo: str = "amerenda/mycroft", image_tag: str = "latest"):
        self.namespace = namespace
        self.image_repo = image_repo
        self.image_tag = image_tag
        self._api = None

    def _get_api(self):
        if self._api is None:
            from kubernetes import client, config
            try:
                config.load_incluster_config()
            except config.ConfigException:
                config.load_kube_config()
            self._api = client.CustomObjectsApi()
        return self._api

    async def submit(
        self,
        agent_type: str,
        task_id: str,
        params: dict[str, Any] | None = None,
    ) -> str:
        """Submit an Argo Workflow for the given agent type and task."""
        workflow = {
            "apiVersion": "argoproj.io/v1alpha1",
            "kind": "Workflow",
            "metadata": {
                "generateName": f"{agent_type}-{task_id[:8]}-",
                "namespace": self.namespace,
            },
            "spec": {
                "workflowTemplateRef": {"name": f"agent-{agent_type}"},
                "arguments": {
                    "parameters": [
                        {"name": "task-id", "value": task_id},
                        {"name": "config", "value": json.dumps(params or {})},
                    ]
                },
            },
        }

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
        return wf_name
