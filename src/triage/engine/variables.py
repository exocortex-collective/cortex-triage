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
    from cortex_utils.triage_config.models import (
        AttachmentFilenameRegexVariable,
        BodyRegexVariable,
        CcRegexVariable,
        Email,
        FromRegexVariable,
        HeaderRegexVariable,
        LLMVariable,
        PatternFieldVariable,
        SubjectRegexVariable,
        ToRegexVariable,
        Variable,
    )

log = structlog.get_logger()

# Regex pattern for {{variable}} template placeholders
# Used by both matcher.py (rendering) and rules.py (validation)
TEMPLATE_PATTERN = re.compile(r"\{\{(\w+)\}\}")


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


def resolve_body_regex(
    email: Email,
    config: BodyRegexVariable,
) -> str | None:
    """Extract value from email body using regex.

    Args:
        email: Email with body.
        config: Body regex configuration.

    Returns:
        Captured group value, or None if body missing or regex doesn't match.
    """
    if not email.body:
        log.debug(
            "Body empty for extraction",
            gmail_id=email.gmail_id,
        )
        return None

    try:
        match = re.search(config.pattern, email.body, re.IGNORECASE)
        if match and len(match.groups()) >= config.group:
            value = match.group(config.group)
            log.debug(
                "Body regex extraction succeeded",
                pattern=config.pattern,
                value=value[:50] if len(value) > 50 else value,
            )
            return value
        log.debug(
            "Body regex did not match",
            pattern=config.pattern,
        )
        return None
    except re.error as e:
        log.error(
            "Invalid extraction regex",
            pattern=config.pattern,
            error=str(e),
        )
        return None


def resolve_from_regex(
    email: Email,
    config: FromRegexVariable,
) -> str | None:
    """Extract value from sender address using regex.

    Args:
        email: Email with from_addr.
        config: From regex configuration.

    Returns:
        Captured group value, or None if regex doesn't match.
    """
    try:
        match = re.search(config.pattern, email.from_addr, re.IGNORECASE)
        if match and len(match.groups()) >= config.group:
            value = match.group(config.group)
            log.debug(
                "From regex extraction succeeded",
                pattern=config.pattern,
                value=value,
            )
            return value
        log.debug(
            "From regex did not match",
            pattern=config.pattern,
            from_addr=email.from_addr,
        )
        return None
    except re.error as e:
        log.error(
            "Invalid extraction regex",
            pattern=config.pattern,
            error=str(e),
        )
        return None


def resolve_to_regex(
    email: Email,
    config: ToRegexVariable,
) -> str | None:
    """Extract value from first matching to_addr using regex.

    Args:
        email: Email with to_addrs.
        config: To regex configuration.

    Returns:
        Captured group value from first matching address, or None.
    """
    if not email.to_addrs:
        log.debug(
            "No to_addrs for extraction",
            gmail_id=email.gmail_id,
        )
        return None

    try:
        for addr in email.to_addrs:
            match = re.search(config.pattern, addr, re.IGNORECASE)
            if match and len(match.groups()) >= config.group:
                value = match.group(config.group)
                log.debug(
                    "To regex extraction succeeded",
                    pattern=config.pattern,
                    addr=addr,
                    value=value,
                )
                return value
        log.debug(
            "To regex did not match any address",
            pattern=config.pattern,
            to_addrs=email.to_addrs[:3],
        )
        return None
    except re.error as e:
        log.error(
            "Invalid extraction regex",
            pattern=config.pattern,
            error=str(e),
        )
        return None


def resolve_cc_regex(
    email: Email,
    config: CcRegexVariable,
) -> str | None:
    """Extract value from first matching cc_addr using regex.

    Args:
        email: Email with cc_addrs.
        config: CC regex configuration.

    Returns:
        Captured group value from first matching address, or None.
    """
    if not email.cc_addrs:
        log.debug(
            "No cc_addrs for extraction",
            gmail_id=email.gmail_id,
        )
        return None

    try:
        for addr in email.cc_addrs:
            match = re.search(config.pattern, addr, re.IGNORECASE)
            if match and len(match.groups()) >= config.group:
                value = match.group(config.group)
                log.debug(
                    "CC regex extraction succeeded",
                    pattern=config.pattern,
                    addr=addr,
                    value=value,
                )
                return value
        log.debug(
            "CC regex did not match any address",
            pattern=config.pattern,
            cc_addrs=email.cc_addrs[:3],
        )
        return None
    except re.error as e:
        log.error(
            "Invalid extraction regex",
            pattern=config.pattern,
            error=str(e),
        )
        return None


def resolve_attachment_filename_regex(
    email: Email,
    config: AttachmentFilenameRegexVariable,
) -> str | None:
    """Extract value from first matching attachment filename using regex.

    Args:
        email: Email with attachment_filenames.
        config: Attachment filename regex configuration.

    Returns:
        Captured group value from first matching filename, or None.
    """
    if not email.attachment_filenames:
        log.debug(
            "No attachment_filenames for extraction",
            gmail_id=email.gmail_id,
        )
        return None

    try:
        for filename in email.attachment_filenames:
            match = re.search(config.pattern, filename, re.IGNORECASE)
            if match and len(match.groups()) >= config.group:
                value = match.group(config.group)
                log.debug(
                    "Attachment filename regex extraction succeeded",
                    pattern=config.pattern,
                    filename=filename,
                    value=value,
                )
                return value
        log.debug(
            "Attachment filename regex did not match",
            pattern=config.pattern,
            filenames=email.attachment_filenames[:3],
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


def resolve_pattern_field(
    email: Email,
    config: PatternFieldVariable,
) -> str | None:
    """Extract value from pattern info field.

    Args:
        email: Email with optional pattern_info.
        config: Pattern field configuration specifying which field to extract.

    Returns:
        Field value as string, or None if pattern_info missing or field not found.
    """
    if email.pattern_info is None:
        log.debug(
            "No pattern_info for extraction",
            gmail_id=email.gmail_id,
        )
        return None

    field_name = config.field
    if not hasattr(email.pattern_info, field_name):
        log.debug(
            "Pattern field not found on pattern_info object",
            field=field_name,
            gmail_id=email.gmail_id,
        )
        return None

    value = getattr(email.pattern_info, field_name)
    if value is not None:
        result = str(value)
        log.debug(
            "Pattern field extraction succeeded",
            field=field_name,
            value=result[:50],
        )
        return result

    log.debug(
        "Pattern field value is None",
        field=field_name,
        gmail_id=email.gmail_id,
    )
    return None


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
        elif var.body_regex:
            value = resolve_body_regex(email, var.body_regex)
        elif var.from_regex:
            value = resolve_from_regex(email, var.from_regex)
        elif var.to_regex:
            value = resolve_to_regex(email, var.to_regex)
        elif var.cc_regex:
            value = resolve_cc_regex(email, var.cc_regex)
        elif var.attachment_filename_regex:
            value = resolve_attachment_filename_regex(
                email, var.attachment_filename_regex
            )
        elif var.llm:
            if llm_client is None:
                raise VariableError(
                    f"LLM variable '{name}' requires LLM client but none provided"
                )
            value = resolve_llm(email, var.llm, llm_client)
        elif var.pattern_field:
            value = resolve_pattern_field(email, var.pattern_field)

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
