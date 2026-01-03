"""Tests for rules loading and validation."""

import tempfile
from pathlib import Path

import pytest

from triage.engine.rules import load_rules, lookup_email_mapping, validate_rules
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

        with pytest.raises(ValueError, match="Empty YAML content"):
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

        # Validation now happens at model construction via model_validator
        with pytest.raises(ValueError, match="only have one of"):
            load_rules(f.name)


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

        # Validation now happens at model construction via model_validator
        with pytest.raises(ValueError, match="must have 'routes'"):
            load_rules(f.name)


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
    """Test loading the example rules.yaml file."""
    rules_path = Path(__file__).parent.parent / "examples" / "rules.yaml.example"
    if not rules_path.exists():
        pytest.skip("Example rules.yaml not found")

    config = load_rules(rules_path)
    errors = validate_rules(config)

    assert "main" in config.chains
    assert "self_note" in config.chains
    assert "llm_classify" in config.chains
    assert errors == []


def test_load_email_mappings() -> None:
    """Test loading email mappings from YAML."""
    yaml_content = """
version: 1
priority_email_mappings:
  notifications@github.com:
    label: Tech/GitHub
    archive: true
  noreply@google.com:
    label: Tech/Google
    mark_read: true
fallback_email_mappings:
  newsletter@example.com:
    label: Marketing/Newsletter
chains:
  main:
    - match: {}
      action:
        label: Uncategorized
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml_content)
        f.flush()

        config = load_rules(f.name)

        # Check priority mappings
        assert len(config.priority_email_mappings) == 2
        assert "notifications@github.com" in config.priority_email_mappings
        assert (
            config.priority_email_mappings["notifications@github.com"].label
            == "Tech/GitHub"
        )
        assert (
            config.priority_email_mappings["notifications@github.com"].archive is True
        )
        assert (
            config.priority_email_mappings["notifications@github.com"].mark_read is None
        )

        assert "noreply@google.com" in config.priority_email_mappings
        assert (
            config.priority_email_mappings["noreply@google.com"].label == "Tech/Google"
        )
        assert config.priority_email_mappings["noreply@google.com"].archive is None
        assert config.priority_email_mappings["noreply@google.com"].mark_read is True

        # Check fallback mappings
        assert len(config.fallback_email_mappings) == 1
        assert "newsletter@example.com" in config.fallback_email_mappings
        assert (
            config.fallback_email_mappings["newsletter@example.com"].label
            == "Marketing/Newsletter"
        )


def test_email_mappings_case_insensitive() -> None:
    """Test that email mappings are normalized to lowercase."""
    yaml_content = """
version: 1
priority_email_mappings:
  Notifications@GitHub.COM:
    label: Tech/GitHub
chains:
  main:
    - match: {}
      action:
        label: Uncategorized
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml_content)
        f.flush()

        config = load_rules(f.name)

        # Should be normalized to lowercase
        assert "notifications@github.com" in config.priority_email_mappings
        assert "Notifications@GitHub.COM" not in config.priority_email_mappings


def test_email_mappings_duplicate_error() -> None:
    """Test that duplicate email addresses across sections raise an error."""
    yaml_content = """
version: 1
priority_email_mappings:
  test@example.com:
    label: Priority
fallback_email_mappings:
  test@example.com:
    label: Fallback
chains:
  main:
    - match: {}
      action:
        label: Uncategorized
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml_content)
        f.flush()

        with pytest.raises(ValueError, match="Duplicate email addresses"):
            load_rules(f.name)


def test_email_mappings_empty_address_error() -> None:
    """Test that empty email addresses raise an error."""
    yaml_content = """
version: 1
priority_email_mappings:
  "":
    label: Test
chains:
  main:
    - match: {}
      action:
        label: Uncategorized
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml_content)
        f.flush()

        with pytest.raises(ValueError, match="Empty email address"):
            load_rules(f.name)


def test_email_mappings_missing_label_error() -> None:
    """Test that email mappings without label raise an error."""
    yaml_content = """
version: 1
priority_email_mappings:
  test@example.com:
    archive: true
chains:
  main:
    - match: {}
      action:
        label: Uncategorized
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml_content)
        f.flush()

        with pytest.raises(ValueError, match="label"):
            load_rules(f.name)


def test_lookup_email_mapping() -> None:
    """Test O(1) hash lookup of email mappings."""
    yaml_content = """
version: 1
priority_email_mappings:
  test@example.com:
    label: Test/Label
fallback_email_mappings:
  fallback@example.com:
    label: Fallback/Label
chains:
  main:
    - match: {}
      action:
        label: Uncategorized
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml_content)
        f.flush()

        config = load_rules(f.name)

        # Test exact match
        result = lookup_email_mapping(
            "test@example.com", config.priority_email_mappings
        )
        assert result is not None
        assert result.label == "Test/Label"

        # Test case-insensitive match
        result = lookup_email_mapping(
            "TEST@EXAMPLE.COM", config.priority_email_mappings
        )
        assert result is not None
        assert result.label == "Test/Label"

        # Test not found
        result = lookup_email_mapping(
            "notfound@example.com", config.priority_email_mappings
        )
        assert result is None

        # Test fallback mappings
        result = lookup_email_mapping(
            "fallback@example.com", config.fallback_email_mappings
        )
        assert result is not None
        assert result.label == "Fallback/Label"
