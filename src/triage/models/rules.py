"""Data models for triage rules."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class IntentConfig(BaseModel):
    """Configuration for a named intent."""

    prompt: str
    model: str = "qwen2.5:0.5b"


class MatchCondition(BaseModel):
    """Conditions for matching an email.

    All specified fields must match (implicit AND). Within a field, lists are
    OR (any value matches). Use any_of/all_of for compound conditions across
    different field types.

    Field lists (OR within field):
        # Any of these exact addresses
        from: ["newsletter@foo.com", "deals@bar.com"]

        # Any of these glob patterns
        from_glob: ["*@mailchimp.com", "*@sendgrid.net"]

    Compound conditions (OR/AND across fields):
        - any_of: At least one sub-condition must match (OR)
        - all_of: All sub-conditions must match (AND) - useful for grouping

    Examples:
        # Match emails from any newsletter domain (using list)
        match:
          from_glob: ["*@mailchimp.com", "*@sendgrid.net", "*@newsletter.com"]

        # Same thing using any_of (more verbose but allows mixing field types)
        match:
          any_of:
            - from_glob: "*@mailchimp.com"
            - from_glob: "*@sendgrid.net"
            - from_addr: "newsletter@example.com"

        # Match GitHub bugs (AND with OR)
        match:
          from_glob: "*@github.com"
          subject_contains: ["bug", "issue", "error"]
    """

    model_config = ConfigDict(populate_by_name=True)

    # Exact matches (single or list - any match)
    from_addr: list[str] | str | None = Field(None, alias="from")
    to_addr: list[str] | str | None = Field(None, alias="to")

    # Glob patterns (single or list - any match)
    from_glob: list[str] | str | None = None
    to_glob: list[str] | str | None = None

    # Substring matches (single or list - any match)
    from_contains: list[str] | str | None = None
    to_contains: list[str] | str | None = None
    subject_contains: list[str] | str | None = None
    body_contains: list[str] | str | None = None

    # Regex (single or list - any match)
    subject_regex: list[str] | str | None = None

    # Intent (LLM-based)
    subject_intent: str | IntentConfig | None = None

    # Other
    has_attachment: bool | None = None

    # Compound conditions
    any_of: list[MatchCondition] | None = None
    all_of: list[MatchCondition] | None = None

    @field_validator("subject_intent", mode="before")
    @classmethod
    def validate_subject_intent(cls, v: Any) -> Any:
        """Parse inline intent dict into an IntentConfig model."""
        if isinstance(v, dict):
            return IntentConfig.model_validate(v)
        return v

    @model_validator(mode="after")
    def validate_compound_not_empty(self) -> MatchCondition:
        """Ensure any_of and all_of have at least one condition if specified."""
        if self.any_of is not None and len(self.any_of) == 0:
            raise ValueError("any_of must contain at least one condition")
        if self.all_of is not None and len(self.all_of) == 0:
            raise ValueError("all_of must contain at least one condition")
        return self


class Action(BaseModel):
    """Action to take when a rule matches.

    Triage handles inbox management only. Workflow dispatch happens via the
    actions service which watches for Cortex/* label events.
    """

    label: str | None = None
    archive: bool | None = None
    mark_read: bool | None = None
    star: bool | None = None


class LLMConfig(BaseModel):
    """Configuration for LLM classification chain."""

    model: str = "qwen2.5:7b"
    prompt_version: str = "v1"


class Rule(BaseModel):
    """A single rule in a chain."""

    match: MatchCondition = Field(
        default_factory=lambda: MatchCondition()  # type: ignore[call-arg]
    )
    action: Action | None = None
    jump: str | None = None
    llm: LLMConfig | None = None
    routes: dict[str, Action] | None = None

    @model_validator(mode="after")
    def check_exclusive_outcomes(self) -> Rule:
        """Ensure a rule has exactly one outcome defined."""
        outcomes_defined = sum(
            x is not None for x in (self.action, self.jump, self.llm)
        )

        if outcomes_defined == 0:
            raise ValueError("Rule must have one of 'action', 'jump', or 'llm'.")

        if outcomes_defined > 1:
            raise ValueError(
                "Rule can only have one of 'action', 'jump', or 'llm' defined."
            )

        if self.llm and not self.routes:
            raise ValueError("LLM rule must have 'routes'.")

        return self


class RulesConfig(BaseModel):
    """Top-level rules configuration."""

    version: int = 1
    label_prefix: str = "Cortex"
    intents: dict[str, IntentConfig] = Field(default_factory=dict)
    chains: dict[str, list[Rule]] = Field(default_factory=dict)


class Email(BaseModel):
    """Email data for rule matching."""

    id: int
    gmail_id: str
    from_addr: str
    to_addr: str
    subject: str
    body: str | None = None
    has_attachment: bool = False


class EvaluationResult(BaseModel):
    """Result of evaluating rules against an email."""

    matched: bool = False
    chain: str | None = None
    rule_index: int | None = None
    action: Action | None = None
    llm_category: str | None = None
    llm_confidence: float | None = None
    llm_reasoning: str | None = None
