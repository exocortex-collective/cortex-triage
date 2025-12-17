"""Variable resolution for dynamic labels.

This module handles extracting variable values from emails for use in
label templates. Variables can be extracted from headers (regex),
subject (regex), or via LLM.

If any variable resolution fails, the rule is considered not matched
and evaluation continues to the next rule.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

import structlog

if TYPE_CHECKING:
    from triage.models.rules import (
        Email,
        HeaderRegexVariable,
        LLMVariable,
        SubjectRegexVariable,
        Variable,
    )

log = structlog.get_logger()


class VariableError(Exception):
    """Raised when variable resolution fails.

    This exception indicates that a required variable could not be extracted,
    which causes the rule to be skipped (fall through to next rule).
    """

    pass


@dataclass
class VariableContext:
    """Holds resolved variable values.

    This context is passed to label template rendering to substitute
    {{variable}} placeholders.
    """

    values: dict[str, str] = field(default_factory=dict)

    def get(self, key: str) -> str | None:
        """Get a variable value by name."""
        return self.values.get(key)


class ExtractionLLMClient(Protocol):
    """Protocol for LLM client used in variable extraction."""

    def extract_value(
        self,
        from_addr: str,
        subject: str,
        body: str | None,
        prompt: str,
        model: str,
    ) -> str | None:
        """Extract a single value from email content.

        Args:
            from_addr: Email sender address.
            subject: Email subject.
            body: Email body (may be None).
            prompt: Prompt template with {from_addr}, {subject}, {body_preview}.
            model: Model name to use.

        Returns:
            Extracted value as string, or None if extraction failed.
        """
        ...


def resolve_header_regex(
    email: Email,
    config: HeaderRegexVariable,
) -> str | None:
    """Extract value from header using regex.

    Args:
        email: Email with headers dict.
        config: Header regex configuration.

    Returns:
        Captured group value, or None if header missing or regex doesn't match.
    """
    # Headers are stored lowercase
    header_value = email.headers.get(config.header.lower(), "")
    if not header_value:
        log.debug(
            "Header not found for extraction",
            header=config.header,
            gmail_id=email.gmail_id,
        )
        return None

    try:
        match = re.search(config.pattern, header_value, re.IGNORECASE)
        if match and len(match.groups()) >= config.group:
            value = match.group(config.group)
            log.debug(
                "Header regex extraction succeeded",
                header=config.header,
                pattern=config.pattern,
                value=value,
            )
            return value
        log.debug(
            "Header regex did not match",
            header=config.header,
            pattern=config.pattern,
            header_value=header_value[:100],
        )
        return None
    except re.error as e:
        log.error(
            "Invalid extraction regex",
            pattern=config.pattern,
            error=str(e),
        )
        return None


def resolve_subject_regex(
    email: Email,
    config: SubjectRegexVariable,
) -> str | None:
    """Extract value from subject using regex.

    Args:
        email: Email with subject.
        config: Subject regex configuration.

    Returns:
        Captured group value, or None if regex doesn't match.
    """
    try:
        match = re.search(config.pattern, email.subject, re.IGNORECASE)
        if match and len(match.groups()) >= config.group:
            value = match.group(config.group)
            log.debug(
                "Subject regex extraction succeeded",
                pattern=config.pattern,
                value=value,
            )
            return value
        log.debug(
            "Subject regex did not match",
            pattern=config.pattern,
            subject=email.subject[:100],
        )
        return None
    except re.error as e:
        log.error(
            "Invalid extraction regex",
            pattern=config.pattern,
            error=str(e),
        )
        return None


def resolve_llm(
    email: Email,
    config: LLMVariable,
    llm_client: ExtractionLLMClient,
) -> str | None:
    """Extract value using LLM.

    Args:
        email: Email data.
        config: LLM extraction configuration.
        llm_client: LLM client implementing extract_value.

    Returns:
        Extracted value, or None if LLM extraction fails.
    """
    value = llm_client.extract_value(
        email.from_addr,
        email.subject,
        email.body,
        config.prompt,
        config.model,
    )
    if value:
        log.debug(
            "LLM extraction succeeded",
            model=config.model,
            value=value[:50] if len(value) > 50 else value,
        )
    else:
        log.debug(
            "LLM extraction returned empty",
            model=config.model,
        )
    return value


def resolve_variables(
    email: Email,
    variables: dict[str, Variable],
    llm_client: ExtractionLLMClient | None = None,
) -> VariableContext:
    """Resolve all variables for a rule.

    All variables must be successfully resolved. If any variable fails,
    VariableError is raised which causes the rule to be skipped.

    Args:
        email: Email data for extraction.
        variables: Dict of variable name -> Variable config.
        llm_client: Optional LLM client for LLM-based extraction.

    Returns:
        VariableContext with resolved values.

    Raises:
        VariableError: If any variable cannot be resolved.
    """
    ctx = VariableContext()

    for name, var in variables.items():
        value: str | None = None

        if var.header_regex:
            value = resolve_header_regex(email, var.header_regex)
        elif var.subject_regex:
            value = resolve_subject_regex(email, var.subject_regex)
        elif var.llm:
            if llm_client is None:
                raise VariableError(
                    f"LLM variable '{name}' requires LLM client but none provided"
                )
            value = resolve_llm(email, var.llm, llm_client)

        # Check if resolution succeeded
        if value is None or value.strip() == "":
            log.debug(
                "Variable resolution failed",
                variable=name,
                gmail_id=email.gmail_id,
            )
            raise VariableError(f"Variable '{name}' could not be resolved")

        ctx.values[name] = value.strip()

    log.debug(
        "All variables resolved",
        variables=list(ctx.values.keys()),
        gmail_id=email.gmail_id,
    )
    return ctx
