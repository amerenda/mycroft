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


# ── per-step resource override (coordinator logic test) ───────────────────────

class TestStepResourceOverride:
    """Verify the coordinator pattern for per-step resource overrides.

    The coordinator reads step.get('resources') and calls model_copy to produce
    a modified manifest before passing it to argo.submit.  These tests exercise
    the AgentManifest / AgentResources model directly (no async coordinator needed).
    """

    def _base_manifest(self) -> AgentManifest:
        return AgentManifest(
            name="researcher",
            resources=AgentResources(memory="2Gi", cpu="2", scratch="5Gi"),
        )

    def _apply(self, manifest: AgentManifest, step_res: dict) -> AgentManifest:
        cur = manifest.resources
        overridden = AgentResources(
            memory=str(step_res.get("memory") or cur.memory),
            cpu=str(step_res.get("cpu") or cur.cpu),
            scratch=str(step_res.get("scratch") or cur.scratch),
        )
        return manifest.model_copy(update={"resources": overridden})

    def test_memory_override_replaces_only_memory(self):
        m = self._apply(self._base_manifest(), {"memory": "4Gi"})
        assert m.resources.memory == "4Gi"
        assert m.resources.cpu == "2"
        assert m.resources.scratch == "5Gi"

    def test_cpu_override_replaces_only_cpu(self):
        m = self._apply(self._base_manifest(), {"cpu": "4"})
        assert m.resources.cpu == "4"
        assert m.resources.memory == "2Gi"

    def test_full_override_sets_all_fields(self):
        m = self._apply(self._base_manifest(), {"memory": "8Gi", "cpu": "8", "scratch": "20Gi"})
        assert m.resources.memory == "8Gi"
        assert m.resources.cpu == "8"
        assert m.resources.scratch == "20Gi"

    def test_empty_dict_keeps_original(self):
        original = self._base_manifest()
        m = self._apply(original, {})
        assert m.resources.memory == original.resources.memory
        assert m.resources.cpu == original.resources.cpu

    def test_original_manifest_is_not_mutated(self):
        original = self._base_manifest()
        self._apply(original, {"memory": "16Gi"})
        assert original.resources.memory == "2Gi"

    def test_overridden_manifest_flows_to_argo_spec(self):
        base = self._base_manifest()
        overridden = self._apply(base, {"memory": "6Gi", "cpu": "3"})
        wf = _submitter()._build_workflow("researcher", "t1", manifest=overridden)
        limits = wf["spec"]["templates"][0]["container"]["resources"]["limits"]
        assert limits["memory"] == "6Gi"
        assert limits["cpu"] == "3"


# ── terminate_task ────────────────────────────────────────────────────────────

async def test_terminate_task_with_no_mapping_returns_false():
    submitter = _submitter()
    assert await submitter.terminate_task("unknown-task-id") is False


async def test_terminate_task_not_tracked_does_not_raise():
    submitter = _submitter()
    result = await submitter.terminate_task("ghost-task")
    assert result is False
