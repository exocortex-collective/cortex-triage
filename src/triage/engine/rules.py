"""Rules loading and configuration."""

import re
from pathlib import Path

import yaml

from triage.models.rules import RulesConfig

# Built-in intents with default prompts
BUILTIN_INTENTS = {
    "archive_request": {
        "prompt": """Does this email subject indicate the sender wants to save,
archive, or bookmark something for later?
Subject: "{subject}"
Answer only: yes or no""",
        "model": "qwen2.5:0.5b",
        "threshold": 0.8,
    },
    "todo_request": {
        "prompt": """Does this email subject indicate a task, reminder, or todo item?
Subject: "{subject}"
Answer only: yes or no""",
        "model": "qwen2.5:0.5b",
        "threshold": 0.8,
    },
    "question": {
        "prompt": """Does this email subject contain a question requiring a response?
Subject: "{subject}"
Answer only: yes or no""",
        "model": "qwen2.5:0.5b",
        "threshold": 0.8,
    },
}


def load_rules(path: str | Path) -> RulesConfig:
    """Load rules from a YAML file.

    Args:
        path: Path to the rules YAML file.

    Returns:
        Parsed RulesConfig object.

    Raises:
        FileNotFoundError: If the rules file doesn't exist.
        ValueError: If the rules file is invalid.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Rules file not found: {path}")

    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if data is None:
        raise ValueError(f"Empty rules file: {path}")

    # Merge built-in intents with user-defined ones
    # User intents override built-ins
    intents = dict(BUILTIN_INTENTS)
    user_intents = data.get("intents") or {}
    for name, config in user_intents.items():
        if name in intents:
            # Merge: user config overrides built-in fields
            intents[name] = {**intents[name], **config}
        else:
            intents[name] = config
    data["intents"] = intents

    return RulesConfig.model_validate(data)


def validate_rules(config: RulesConfig) -> list[str]:
    """Validate rules configuration.

    Returns a list of validation errors (empty if valid).
    """
    errors: list[str] = []

    # Must have a 'main' chain
    if "main" not in config.chains:
        errors.append("Rules must have a 'main' chain")

    # Validate jump targets and intent references
    # Note: Rule model_validator handles action/jump/llm exclusivity and llm+routes
    for chain_name, rules in config.chains.items():
        for i, rule in enumerate(rules):
            if rule.jump and rule.jump not in config.chains:
                errors.append(
                    f"Chain '{chain_name}' rule {i}: "
                    f"jump target '{rule.jump}' does not exist"
                )

            # Validate intent references
            intent = rule.match.subject_intent
            if isinstance(intent, str) and intent not in config.intents:
                errors.append(
                    f"Chain '{chain_name}' rule {i}: " f"unknown intent '{intent}'"
                )

            # Validate regex patterns
            if rule.match.subject_regex:
                patterns = (
                    [rule.match.subject_regex]
                    if isinstance(rule.match.subject_regex, str)
                    else rule.match.subject_regex
                )
                for pattern in patterns:
                    try:
                        re.compile(pattern)
                    except re.error as e:
                        errors.append(
                            f"Chain '{chain_name}' rule {i}: "
                            f"invalid subject_regex '{pattern}': {e}"
                        )

    return errors
