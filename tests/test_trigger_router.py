"""Tests for coordinator/trigger_router.py — event routing and manifest registration."""

from __future__ import annotations

from coordinator.trigger_router import TriggerRouter, _extract_prompt_text


_CODER_YAML = """
name: coder
role: Software engineer
goal: Write code
model: qwen3:14b
tools:
  - git
  - shell
max_iterations: 8
triggers:
  - event: task.created
    filter:
      agent_type: coder
"""


# ── _extract_prompt_text ──────────────────────────────────────────────────────

class TestExtractPromptText:
    def test_plain_text_returned_stripped(self):
        text = "  You are a helpful assistant.\nDo things.  "
        assert _extract_prompt_text(text) == "You are a helpful assistant.\nDo things."

    def test_old_python_format_extracts_triple_quoted_body(self):
        old_fmt = 'SYSTEM_SUPPLEMENT = """\nYou are a research agent.\nUse web search.\n"""'
        result = _extract_prompt_text(old_fmt)
        assert result == "You are a research agent.\nUse web search."

    def test_no_system_supplement_key_returns_text(self):
        text = "Just a plain prompt\nwith newlines"
        assert _extract_prompt_text(text) == text.strip()

    def test_empty_string_returns_empty(self):
        assert _extract_prompt_text("") == ""


# ── register / unregister ─────────────────────────────────────────────────────

class TestTriggerRouterRegister:
    def test_register_valid_yaml_returns_manifest(self):
        router = TriggerRouter()
        manifest = router.register("coder", _CODER_YAML)
        assert manifest is not None
        assert manifest.name == "coder"
        assert manifest.model == "qwen3:14b"
        assert "git" in manifest.tools

    def test_register_stores_prompts(self):
        router = TriggerRouter()
        router.register("coder", _CODER_YAML, "You are an expert coder.")
        assert router.get_prompts("coder") == "You are an expert coder."

    def test_register_empty_yaml_returns_none(self):
        router = TriggerRouter()
        assert router.register("broken", "") is None

    def test_register_yaml_list_not_dict_returns_none(self):
        router = TriggerRouter()
        # YAML that parses to a list, not a dict
        assert router.register("broken", "- list item\n- another") is None

    def test_register_updates_existing_agent(self):
        router = TriggerRouter()
        router.register("coder", _CODER_YAML)
        updated = _CODER_YAML.replace("qwen3:14b", "mistral:7b")
        router.register("coder", updated)
        assert router.manifests["coder"].model == "mistral:7b"

    def test_register_name_defaults_to_key_if_absent_in_yaml(self):
        yaml_without_name = "role: Tester\ngoal: Test\n"
        router = TriggerRouter()
        manifest = router.register("fallback-name", yaml_without_name)
        assert manifest is not None
        assert manifest.name == "fallback-name"

    def test_unregister_removes_manifest_and_prompts(self):
        router = TriggerRouter()
        router.register("coder", _CODER_YAML, "some prompts")
        router.unregister("coder")
        assert "coder" not in router.manifests
        assert router.get_prompts("coder") == ""

    def test_unregister_nonexistent_is_noop(self):
        router = TriggerRouter()
        router.unregister("nobody")  # must not raise

    def test_get_prompts_returns_empty_when_absent(self):
        router = TriggerRouter()
        assert router.get_prompts("nobody") == ""


# ── route ─────────────────────────────────────────────────────────────────────

class TestTriggerRouterRoute:
    def _coder_router(self) -> TriggerRouter:
        router = TriggerRouter()
        router.register("coder", _CODER_YAML)
        return router

    def test_matching_event_and_filter_returns_agent(self):
        router = self._coder_router()
        assert router.route("task.created", {"agent_type": "coder"}) == ["coder"]

    def test_wrong_event_returns_empty(self):
        router = self._coder_router()
        assert router.route("task.updated", {"agent_type": "coder"}) == []

    def test_filter_mismatch_returns_empty(self):
        router = self._coder_router()
        assert router.route("task.created", {"agent_type": "researcher"}) == []

    def test_no_agents_registered_returns_empty(self):
        assert TriggerRouter().route("task.created", {}) == []

    def test_list_filter_value_matches_any_element(self):
        yaml = "name: multi\ntriggers:\n  - event: repo.push\n    filter:\n      branch: [main, master]\n"
        router = TriggerRouter()
        router.register("multi", yaml)
        assert router.route("repo.push", {"branch": "main"}) == ["multi"]
        assert router.route("repo.push", {"branch": "master"}) == ["multi"]
        assert router.route("repo.push", {"branch": "develop"}) == []

    def test_multiple_agents_can_match_same_event(self):
        router = TriggerRouter()
        router.register("a", "name: a\ntriggers:\n  - event: ping\n")
        router.register("b", "name: b\ntriggers:\n  - event: ping\n")
        assert set(router.route("ping", {})) == {"a", "b"}

    def test_empty_filter_matches_any_payload(self):
        router = TriggerRouter()
        router.register("any", "name: any\ntriggers:\n  - event: thing\n    filter: {}\n")
        assert router.route("thing", {"anything": "here"}) == ["any"]
        assert router.route("thing", {}) == ["any"]

    def test_get_manifest_returns_registered_manifest(self):
        router = self._coder_router()
        m = router.get_manifest("coder")
        assert m is not None
        assert m.name == "coder"

    def test_get_manifest_returns_none_for_unknown(self):
        assert TriggerRouter().get_manifest("nobody") is None
