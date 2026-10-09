"""Lane options (2026-10-09): the delegate names its model, effort and CLI explicitly.

Defaults are the owner's pick (Opus 5.5, effort high, the box's own `claude`); every one
overrides per run through the environment. No SDK import: the fields dict is asserted
as data, exactly like the rest of this suite.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import delegate_coder as dc  # noqa: E402

LANE_VARS = ("DELEGATE_MODEL", "DELEGATE_EFFORT", "DELEGATE_CLI")


@pytest.fixture
def clean_env(monkeypatch):
    for var in LANE_VARS:
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def test_defaults_name_opus_high_and_box_claude(clean_env, tmp_path):
    clean_env.setattr(dc.shutil, "which", lambda name: "/usr/bin/claude" if name == "claude" else None)
    fields = dc.build_options_fields(tmp_path, "rules")
    assert fields["model"] == "claude-opus-5-5"
    assert fields["effort"] == "high"
    assert fields["cli_path"] == "/usr/bin/claude"


def test_env_overrides_every_lane_field(clean_env, tmp_path):
    clean_env.setenv("DELEGATE_MODEL", "claude-fable-5-1")
    clean_env.setenv("DELEGATE_EFFORT", "max")
    clean_env.setenv("DELEGATE_CLI", "/opt/claude/bin/claude")
    clean_env.setattr(dc.shutil, "which", lambda name: "/usr/bin/claude")
    fields = dc.build_options_fields(tmp_path, "rules")
    assert (fields["model"], fields["effort"], fields["cli_path"]) == (
        "claude-fable-5-1", "max", "/opt/claude/bin/claude")


def test_no_box_claude_falls_back_to_bundled_cli(clean_env, tmp_path):
    clean_env.setattr(dc.shutil, "which", lambda name: None)
    assert dc.build_options_fields(tmp_path, "rules")["cli_path"] is None


def test_empty_env_values_do_not_blank_the_defaults(clean_env, tmp_path):
    for var in LANE_VARS:
        clean_env.setenv(var, "")
    clean_env.setattr(dc.shutil, "which", lambda name: "/usr/bin/claude")
    fields = dc.build_options_fields(tmp_path, "rules")
    assert (fields["model"], fields["effort"], fields["cli_path"]) == (
        "claude-opus-5-5", "high", "/usr/bin/claude")


def test_lane_fields_ride_beside_the_unchanged_contract(clean_env, tmp_path):
    fields = dc.build_options_fields(tmp_path, "rules")
    assert fields["permission_mode"] == "bypassPermissions"
    assert fields["setting_sources"] == ["user"]
    assert fields["system_prompt"]["append"] == "rules"
