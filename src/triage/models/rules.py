"""Data models for triage rules."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class IntentConfig(BaseModel):
    """Configuration for a named intent."""

    prompt: str
    model: str = "qwen2.5:0.5b"
    threshold: float = 0.8


class MatchCondition(BaseModel):
    """Conditions for matching an email."""

    model_config = ConfigDict(populate_by_name=True)

    # Exact matches
    from_addr: str | None = Field(None, alias="from")
    to_addr: str | None = Field(None, alias="to")

    # Glob patterns
    from_glob: str | None = None
    to_glob: str | None = None

    # Substring matches
    from_contains: list[str] | str | None = None
    to_contains: list[str] | str | None = None
    subject_contains: list[str] | str | None = None
    body_contains: list[str] | str | None = None

    # Regex
    subject_regex: str | None = None

    # Intent (LLM-based)
    subject_intent: str | IntentConfig | None = None

    # Other
    has_attachment: bool | None = None

    @field_validator("subject_intent", mode="before")
    @classmethod
    def validate_subject_intent(cls, v: Any) -> Any:
        """Parse inline intent dict into an IntentConfig model."""
        if isinstance(v, dict):
            return IntentConfig.model_validate(v)
        return v


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
    def check_exclusive_outcomes(self) -> "Rule":
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
