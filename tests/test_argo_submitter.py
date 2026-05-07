"""Tests for coordinator/argo_submitter.py — workflow spec building and termination."""

from __future__ import annotations

from common.models import AgentManifest, AgentResources
from coordinator.argo_submitter import ArgoSubmitter


def _submitter(**kw) -> ArgoSubmitter:
    return ArgoSubmitter(
        namespace="mycroft",
        image_repo="amerenda/mycroft",
        image_tag="abc123",
        **kw,
    )


# ── _build_workflow — legacy path (no manifest) ───────────────────────────────

class TestBuildWorkflowWithoutManifest:
    def test_uses_workflow_template_ref(self):
        wf = _submitter()._build_workflow("coder", "task-abc-123")
        assert "workflowTemplateRef" in wf["spec"]
        assert wf["spec"]["workflowTemplateRef"]["name"] == "agent-coder"

    def test_no_templates_key_in_spec(self):
        wf = _submitter()._build_workflow("coder", "task-abc")
        assert "templates" not in wf["spec"]

    def test_generate_name_is_agent_type_prefix(self):
        wf = _submitter()._build_workflow("researcher", "abcd1234xyz")
        assert wf["metadata"]["generateName"] == "researcher-abcd1234-"

    def test_namespace_set_correctly(self):
        wf = ArgoSubmitter(namespace="staging")._build_workflow("coder", "t1")
        assert wf["metadata"]["namespace"] == "staging"

    def test_arguments_include_task_id(self):
        wf = _submitter()._build_workflow("coder", "my-task-id")
        params = {p["name"]: p["value"] for p in wf["spec"]["arguments"]["parameters"]}
        assert params["task-id"] == "my-task-id"

    def test_arguments_include_config_json(self):
        wf = _submitter()._build_workflow("coder", "t1", params={"key": "val"})
        params = {p["name"]: p["value"] for p in wf["spec"]["arguments"]["parameters"]}
        import json
        assert json.loads(params["config"])["key"] == "val"

    def test_kind_is_workflow(self):
        wf = _submitter()._build_workflow("coder", "t1")
        assert wf["kind"] == "Workflow"
        assert wf["apiVersion"] == "argoproj.io/v1alpha1"


# ── _build_workflow — inline path (with manifest) ─────────────────────────────

class TestBuildWorkflowWithManifest:
    def _manifest(self, **kw) -> AgentManifest:
        return AgentManifest(
            name="coder",
            resources=AgentResources(memory="1Gi", cpu="2", scratch="10Gi"),
            **kw,
        )

    def test_inline_spec_has_templates_not_template_ref(self):
        wf = _submitter()._build_workflow("coder", "t1", manifest=self._manifest())
        assert "templates" in wf["spec"]
        assert "workflowTemplateRef" not in wf["spec"]

    def test_image_repo_and_tag_in_container_image(self):
        wf = _submitter()._build_workflow("coder", "t1", manifest=self._manifest())
        image = wf["spec"]["templates"][0]["container"]["image"]
        assert "amerenda/mycroft" in image
        assert "abc123" in image

    def test_resource_limits_from_manifest(self):
        wf = _submitter()._build_workflow("coder", "t1", manifest=self._manifest())
        limits = wf["spec"]["templates"][0]["container"]["resources"]["limits"]
        assert limits["memory"] == "1Gi"
        assert limits["cpu"] == "2"

    def test_entrypoint_matches_template_name(self):
        wf = _submitter()._build_workflow("coder", "t1", manifest=self._manifest())
        entrypoint = wf["spec"]["entrypoint"]
        template_name = wf["spec"]["templates"][0]["name"]
        assert entrypoint == template_name

    def test_env_includes_task_id_and_agent_type(self):
        wf = _submitter()._build_workflow("coder", "t1", manifest=self._manifest())
        env = wf["spec"]["templates"][0]["container"]["env"]
        env_names = {e["name"] for e in env}
        assert "TASK_ID" in env_names
        assert "MYCROFT_AGENT_TYPE" in env_names

    def test_retry_strategy_present(self):
        wf = _submitter()._build_workflow("coder", "t1", manifest=self._manifest())
        assert "retryStrategy" in wf["spec"]["templates"][0]

    def test_default_resources_used_when_manifest_has_defaults(self):
        manifest = AgentManifest(name="coder")  # uses AgentResources defaults
        wf = _submitter()._build_workflow("coder", "t1", manifest=manifest)
        limits = wf["spec"]["templates"][0]["container"]["resources"]["limits"]
        assert limits["memory"] == "512Mi"  # AgentResources default
        assert limits["cpu"] == "1"


# ── terminate_task ────────────────────────────────────────────────────────────

async def test_terminate_task_with_no_mapping_returns_false():
    submitter = _submitter()
    assert await submitter.terminate_task("unknown-task-id") is False


async def test_terminate_task_not_tracked_does_not_raise():
    submitter = _submitter()
    result = await submitter.terminate_task("ghost-task")
    assert result is False
