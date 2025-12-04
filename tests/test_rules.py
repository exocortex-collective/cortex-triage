"""Tests for rules loading and validation."""

import tempfile
from pathlib import Path

import pytest

from triage.engine.rules import load_rules, validate_rules
from triage.models.rules import RulesConfig


def test_load_rules_from_yaml() -> None:
    """Test loading rules from a YAML file."""
    yaml_content = """
version: 1
label_prefix: "Test"
chains:
  main:
    - match:
        from: "test@example.com"
      action:
        label: "Test"
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml_content)
        f.flush()

        config = load_rules(f.name)

        assert config.version == 1
        assert config.label_prefix == "Test"
        assert "main" in config.chains
        assert len(config.chains["main"]) == 1


def test_load_rules_file_not_found() -> None:
    """Test loading rules from non-existent file."""
    with pytest.raises(FileNotFoundError):
        load_rules("/nonexistent/path/rules.yaml")


def test_load_rules_empty_file() -> None:
    """Test loading rules from empty file."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write("")
        f.flush()

        with pytest.raises(ValueError, match="Empty rules file"):
            load_rules(f.name)


def test_builtin_intents_loaded() -> None:
    """Test that built-in intents are automatically loaded."""
    yaml_content = """
version: 1
chains:
  main:
    - match: {}
      action:
        label: "Test"
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml_content)
        f.flush()

        config = load_rules(f.name)

        assert "archive_request" in config.intents
        assert "todo_request" in config.intents
        assert "question" in config.intents


def test_user_intents_override_builtin() -> None:
    """Test that user-defined intents override built-ins."""
    yaml_content = """
version: 1
intents:
  archive_request:
    prompt: "Custom prompt for {subject}"
    model: "custom-model"
chains:
  main:
    - match: {}
      action:
        label: "Test"
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml_content)
        f.flush()

        config = load_rules(f.name)

        assert config.intents["archive_request"].prompt == "Custom prompt for {subject}"
        assert config.intents["archive_request"].model == "custom-model"


def test_validate_rules_missing_main_chain() -> None:
    """Test validation fails without main chain."""
    config = RulesConfig(chains={"other": []})
    errors = validate_rules(config)
    assert any("main" in e for e in errors)


def test_validate_rules_invalid_jump_target() -> None:
    """Test validation fails with invalid jump target."""
    yaml_content = """
version: 1
chains:
  main:
    - match: {}
      jump: nonexistent
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml_content)
        f.flush()

        config = load_rules(f.name)
        errors = validate_rules(config)
        assert any("nonexistent" in e for e in errors)


def test_validate_rules_action_and_jump() -> None:
    """Test validation fails when rule has both action and jump."""
    yaml_content = """
version: 1
chains:
  main:
    - match: {}
      action:
        label: "Test"
      jump: main
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml_content)
        f.flush()

        config = load_rules(f.name)
        errors = validate_rules(config)
        assert any("both" in e.lower() for e in errors)


def test_validate_rules_llm_without_routes() -> None:
    """Test validation fails when LLM rule has no routes."""
    yaml_content = """
version: 1
chains:
  main:
    - match: {}
      llm:
        model: "test"
        prompt_version: "v1"
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml_content)
        f.flush()

        config = load_rules(f.name)
        errors = validate_rules(config)
        assert any("routes" in e.lower() for e in errors)


def test_validate_rules_unknown_intent() -> None:
    """Test validation fails when referencing unknown intent."""
    yaml_content = """
version: 1
chains:
  main:
    - match:
        subject_intent: "unknown_intent"
      action:
        label: "Test"
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml_content)
        f.flush()

        config = load_rules(f.name)
        errors = validate_rules(config)
        assert any("unknown_intent" in e for e in errors)


def test_validate_rules_valid_config() -> None:
    """Test validation passes for valid config."""
    yaml_content = """
version: 1
chains:
  main:
    - match:
        from: "test@example.com"
      jump: other
    - match: {}
      action:
        label: "Fallback"
  other:
    - match: {}
      action:
        label: "Other"
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml_content)
        f.flush()

        config = load_rules(f.name)
        errors = validate_rules(config)
        assert errors == []


def test_load_sample_rules() -> None:
    """Test loading the sample rules.yaml file."""
    rules_path = Path(__file__).parent.parent / "config" / "rules.yaml"
    if not rules_path.exists():
        pytest.skip("Sample rules.yaml not found")

    config = load_rules(rules_path)
    errors = validate_rules(config)

    assert "main" in config.chains
    assert "self_note" in config.chains
    assert "llm_classify" in config.chains
    assert errors == []
