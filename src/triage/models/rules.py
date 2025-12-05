"""Data models for triage rules."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


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
    from_contains: str | None = None
    to_contains: str | None = None
    subject_contains: list[str] | str | None = None
    body_contains: list[str] | str | None = None

    # Regex
    subject_regex: str | None = None

    # Intent (LLM-based)
    subject_intent: str | dict[str, Any] | None = None

    # Other
    has_attachment: bool | None = None


class Action(BaseModel):
    """Action to take when a rule matches."""

    label: str | None = None
    archive: bool | None = None
    mark_read: bool | None = None
    star: bool | None = None
    workflow: str | None = None
    webhook: str | None = None


class LLMConfig(BaseModel):
    """Configuration for LLM classification chain."""

    model: str = "qwen2.5:7b"
    prompt_version: str = "v1"


class LLMRoutes(BaseModel):
    """Routes for LLM classification results."""

    routes: dict[str, Action] = Field(default_factory=dict)


class Rule(BaseModel):
    """A single rule in a chain."""

    match: MatchCondition = Field(
        default_factory=lambda: MatchCondition()  # type: ignore[call-arg]
    )
    action: Action | None = None
    jump: str | None = None
    llm: LLMConfig | None = None
    routes: dict[str, Action] | None = None


class IntentConfig(BaseModel):
    """Configuration for a named intent."""

    prompt: str
    model: str = "qwen2.5:0.5b"
    threshold: float = 0.8


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
