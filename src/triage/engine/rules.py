"""Rules loading and configuration."""

import re
from pathlib import Path

import yaml

from triage.engine.variables import TEMPLATE_PATTERN
from triage.models.rules import BUILTIN_PROMPTS_DATA, RulesConfig

# Category descriptions for prompt generation
CATEGORY_DESCRIPTIONS = {
    "automated_noise": "GitHub notifications, service alerts, social media, marketing",
    "human_request": "Personal emails requiring response (friends, family, colleagues)",
    "action_item": "Has deadline (invoices, permission slips, renewals, bills)",
    "wrong_email": "Misdirected mail",
    "subscription": "Newsletters, marketing, recurring service emails",
    "school": "Kids' school communications",
}

# Built-in intents with default prompts
# These can be overridden or extended by defining intents in rules.yaml
BUILTIN_INTENTS = {
    "archive_request": {
        "prompt": """Does this email subject indicate the sender wants to save,
archive, or bookmark something for later?
Subject: "{subject}"
Answer only: yes or no""",
        "model": "qwen2.5:0.5b",
    },
    "todo_request": {
        "prompt": """Does this email subject indicate a task, reminder, or todo item?
Subject: "{subject}"
Answer only: yes or no""",
        "model": "qwen2.5:0.5b",
    },
    "question": {
        "prompt": """Does this email subject contain a question requiring a response?
Subject: "{subject}"
Answer only: yes or no""",
        "model": "qwen2.5:0.5b",
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

    # Merge built-in prompts with user-defined ones
    # User prompts override built-ins
    prompts = {k: dict(v) for k, v in BUILTIN_PROMPTS_DATA.items()}
    user_prompts = data.get("prompts") or {}
    for version, config in user_prompts.items():
        if version in prompts:
            prompts[version] = {**prompts[version], **config}
        else:
            prompts[version] = config
    data["prompts"] = prompts

    return RulesConfig.model_validate(data)


def format_classification_prompt(
    config: RulesConfig,
    prompt_version: str,
    from_addr: str,
    subject: str,
    body: str,
    extract_fields: list[str] | None = None,
) -> tuple[str, str]:
    """Format a classification prompt with email data.

    Args:
        config: Rules configuration containing prompts.
        prompt_version: Version of prompt to use (e.g., "v1").
        from_addr: Email sender address.
        subject: Email subject.
        body: Email body text.
        extract_fields: Optional list of field names to extract
            alongside classification.

    Returns:
        Tuple of (formatted_prompt, model_name).

    Raises:
        ValueError: If prompt_version not found in config.
    """
    if prompt_version not in config.prompts:
        raise ValueError(f"Unknown prompt version: {prompt_version}")

    prompt_config = config.prompts[prompt_version]

    # Format categories with descriptions
    categories_text = "\n".join(
        f"- {cat}: {CATEGORY_DESCRIPTIONS.get(cat, cat)}"
        for cat in prompt_config.categories
    )

    # Format the template
    body_preview = body[:500] if body else "(empty)"
    formatted = prompt_config.template.format(
        categories=categories_text,
        from_addr=from_addr,
        subject=subject,
        body_preview=body_preview,
    )

    # Add extraction instructions if fields specified
    if extract_fields:
        fields_str = ", ".join(extract_fields)
        extracted_obj = ", ".join(f'"{f}": "..."' for f in extract_fields)
        extract_instruction = f"""

Also extract these fields from the email:
{fields_str}

Include an "extracted" object in your JSON response with the field values:
{{"category": "...", "confidence": 0.0-1.0, "reasoning": "...",\
 "extracted": {{{extracted_obj}}}}}"""
        formatted += extract_instruction

    return formatted, prompt_config.model


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

            # Validate regex patterns in match conditions
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

            # Validate variables
            defined_vars: set[str] = set()
            if rule.variables:
                for var_name, var in rule.variables.items():
                    # Variable name must be a valid identifier
                    if not var_name.isidentifier():
                        errors.append(
                            f"Chain '{chain_name}' rule {i}: "
                            f"invalid variable name '{var_name}' "
                            "(must be valid identifier)"
                        )

                    defined_vars.add(var_name)

                    # Validate regex patterns in variable extractors
                    if var.header_regex:
                        try:
                            re.compile(var.header_regex.pattern)
                        except re.error as e:
                            errors.append(
                                f"Chain '{chain_name}' rule {i}: "
                                f"invalid header_regex pattern for '{var_name}': {e}"
                            )
                    if var.subject_regex:
                        try:
                            re.compile(var.subject_regex.pattern)
                        except re.error as e:
                            errors.append(
                                f"Chain '{chain_name}' rule {i}: "
                                f"invalid subject_regex pattern for '{var_name}': {e}"
                            )

            # Add LLM extract fields to defined vars
            if rule.llm and rule.llm.extract:
                for field in rule.llm.extract:
                    defined_vars.add(field)

            # Helper to find undefined variables in a label template
            def get_undefined_vars(
                label: str, defined: set[str] = defined_vars
            ) -> set[str]:
                return set(TEMPLATE_PATTERN.findall(label)) - defined

            # Validate label templates reference only defined variables
            if (
                rule.action
                and rule.action.label
                and (undefined := get_undefined_vars(rule.action.label))
            ):
                errors.append(
                    f"Chain '{chain_name}' rule {i}: "
                    f"label template references undefined variables: {undefined}"
                )

            # Validate route action label templates
            if rule.routes:
                for route_name, route_action in rule.routes.items():
                    if route_action.label and (
                        undefined := get_undefined_vars(route_action.label)
                    ):
                        errors.append(
                            f"Chain '{chain_name}' rule {i} "
                            f"route '{route_name}': label template "
                            f"references undefined variables: {undefined}"
                        )

    return errors
