"""The example run files and policies load, and a spec refuses settings that can carry a key."""
from pathlib import Path

import pytest

from provrun.backends.litellm import SETTINGS
from provrun.spec import AgentSpec, load_run_file

FILES = sorted((Path(__file__).parent.parent / "examples").glob("*.yaml"))
POLICIES = [p for p in FILES if p.name.endswith("-policy.yaml")]
EXAMPLES = [p for p in FILES if p not in POLICIES]


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_example_loads(path):
    run, agent = load_run_file(path)
    assert {"org", "domain", "env", "actor"} <= set(run)
    if agent.backend == "litellm":
        assert set(agent.settings) <= SETTINGS


@pytest.mark.parametrize("path", POLICIES, ids=lambda p: p.name)
def test_example_policy_loads(path):
    from provevi import TelemetryPolicy

    assert TelemetryPolicy.load(path).live_level() == "names"


def test_real_model_examples_exist():
    assert {"claude-run.yaml", "claude-agent.yaml"} <= {p.name for p in EXAMPLES}


@pytest.mark.parametrize("setting", ["api_key", "extra_headers", "extra_body", "Authorization", "base_url"])
def test_spec_refuses_setting_that_can_carry_a_key(setting):
    # The spec goes into run_created, so a key in a setting would land in the evidence.
    with pytest.raises(ValueError, match="credential"):
        AgentSpec(name="a", backend="pydantic-ai", model="m", prompt="p", settings={setting: "sk-secret"})


def test_spec_takes_ordinary_settings():
    AgentSpec(name="a", backend="litellm", model="m", prompt="p",
              settings={"max_tokens": 10, "temperature": 0, "reasoning_effort": "low"})
