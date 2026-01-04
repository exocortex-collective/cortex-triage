"""Rules loading and configuration."""

import logging
from pathlib import Path

from cortex_utils.triage_config import load_rules_from_string
from cortex_utils.triage_config.importer import CATEGORY_DESCRIPTIONS
from cortex_utils.triage_config.models import EmailMappingAction, RulesConfig

logger = logging.getLogger(__name__)


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
        yaml_content = f.read()

    return load_rules_from_string(yaml_content)


def lookup_email_mapping(
    from_addr: str, mappings: dict[str, EmailMappingAction]
) -> EmailMappingAction | None:
    """O(1) hash lookup for email mapping, case-insensitive.

    Args:
        from_addr: Sender email address to look up
        mappings: Email mappings dict (already normalized to lowercase keys)

    Returns:
        EmailMappingAction if found, None otherwise
    """
    normalized = from_addr.lower().strip()
    return mappings.get(normalized)


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
