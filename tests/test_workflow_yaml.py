"""Tests for workflow YAML structure — validates that workflow files parse correctly."""

from __future__ import annotations

from pathlib import Path

import yaml


_WORKFLOWS_DIR = Path(__file__).resolve().parent.parent / "workflows"


def _load_yaml(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


class TestResearchNewWorkflow:
    """research-new.yaml must satisfy the coordinator's pipeline loader expectations."""

    def setup_method(self):
        self.data = _load_yaml(_WORKFLOWS_DIR / "research-new.yaml")

    def test_file_is_valid_yaml(self):
        assert isinstance(self.data, dict)

    def test_pipeline_json_key_present(self):
        assert "pipeline_json" in self.data

    def test_pipeline_json_is_dict(self):
        assert isinstance(self.data["pipeline_json"], dict)

    def test_steps_present(self):
        assert "steps" in self.data["pipeline_json"]

    def test_steps_is_list(self):
        assert isinstance(self.data["pipeline_json"]["steps"], list)

    def test_three_steps(self):
        assert len(self.data["pipeline_json"]["steps"]) == 3

    def test_first_step_is_web_search(self):
        assert self.data["pipeline_json"]["steps"][0]["agent"] == "web-search"

    def test_second_step_is_researcher(self):
        assert self.data["pipeline_json"]["steps"][1]["agent"] == "researcher"

    def test_third_step_is_report_writer(self):
        assert self.data["pipeline_json"]["steps"][2]["agent"] == "report-writer"

    def test_description_present(self):
        assert "description" in self.data["pipeline_json"]
        assert self.data["pipeline_json"]["description"]

    def test_content_field_present(self):
        assert "content" in self.data
        assert self.data["content"]

    def test_each_step_has_agent_key(self):
        for step in self.data["pipeline_json"]["steps"]:
            assert "agent" in step, f"Step missing 'agent' key: {step}"

    def test_agent_names_are_strings(self):
        for step in self.data["pipeline_json"]["steps"]:
            assert isinstance(step["agent"], str)


class TestAllWorkflowFiles:
    """Every .yaml in workflows/ must be valid YAML with a dict at the root."""

    def test_all_yaml_files_parse(self):
        yaml_files = list(_WORKFLOWS_DIR.glob("*.yaml"))
        assert len(yaml_files) > 0, "No workflow YAML files found"
        for path in yaml_files:
            if path.name == ".gitkeep":
                continue
            data = _load_yaml(path)
            assert isinstance(data, dict), f"{path.name} root must be a dict"
