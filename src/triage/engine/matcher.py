"""Match condition evaluation for rules engine."""

import fnmatch
import re
from typing import Protocol

import structlog

from triage.models.rules import (
    Action,
    Email,
    EvaluationResult,
    MatchCondition,
    RulesConfig,
)

log = structlog.get_logger()


class LLMClient(Protocol):
    """Protocol for LLM client used in intent matching."""

    def check_intent(self, subject: str, prompt: str, model: str) -> bool:
        """Check if subject matches an intent.

        Args:
            subject: Email subject to check.
            prompt: Prompt template with {subject} placeholder.
            model: Model name to use.

        Returns:
            True if the intent matches, False otherwise.
        """
        ...

    def classify(
        self, from_addr: str, subject: str, body: str, model: str, prompt_version: str
    ) -> tuple[str, float, str]:
        """Classify an email.

        Args:
            from_addr: Sender address.
            subject: Email subject.
            body: Email body.
            model: Model name to use.
            prompt_version: Prompt version to use.

        Returns:
            Tuple of (category, confidence, reasoning).
        """
        ...


def _normalize(s: str | None) -> str:
    """Normalize string for comparison."""
    return (s or "").lower().strip()


def _match_glob(pattern: str, value: str) -> bool:
    """Match a glob pattern against a value (case-insensitive)."""
    return fnmatch.fnmatch(_normalize(value), _normalize(pattern))


def _match_contains(needles: list[str] | str | None, haystack: str) -> bool:
    """Check if any needle is in haystack (case-insensitive)."""
    if needles is None:
        return False
    if isinstance(needles, str):
        needles = [needles]
    haystack_lower = _normalize(haystack)
    return any(_normalize(needle) in haystack_lower for needle in needles)


def _match_regex(pattern: str | None, value: str) -> bool:
    """Match a regex pattern against a value."""
    if pattern is None:
        return False
    try:
        return bool(re.search(pattern, value, re.IGNORECASE))
    except re.error:
        log.error(
            "Invalid regex pattern encountered during matching",
            pattern=pattern,
            exc_info=True,
        )
        return False


def _apply_label_prefix(action: Action, prefix: str) -> Action:
    """Apply label prefix to an action if it has a label."""
    if action.label and prefix:
        return action.model_copy(update={"label": f"{prefix}/{action.label}"})
    return action


def matches_condition(
    condition: MatchCondition,
    email: Email,
    llm_client: LLMClient | None = None,
    intents: dict | None = None,
) -> bool:
    """Check if an email matches a condition.

    Args:
        condition: The match condition to evaluate.
        email: The email to match against.
        llm_client: Optional LLM client for intent matching.
        intents: Intent configurations for intent matching.

    Returns:
        True if all specified conditions match, False otherwise.
        Empty condition (no fields set) matches everything.
    """
    # Pre-normalize email fields for efficiency
    from_addr_norm = _normalize(email.from_addr)
    to_addr_norm = _normalize(email.to_addr)
    subject_norm = _normalize(email.subject)
    body_norm = _normalize(email.body) if email.body else None

    # Check each condition type - all specified must match

    # Exact from match
    if condition.from_addr is not None and from_addr_norm != _normalize(
        condition.from_addr
    ):
        return False

    # Exact to match
    if condition.to_addr is not None and to_addr_norm != _normalize(condition.to_addr):
        return False

    # Glob patterns
    if condition.from_glob is not None and not fnmatch.fnmatch(
        from_addr_norm, _normalize(condition.from_glob)
    ):
        return False

    if condition.to_glob is not None and not fnmatch.fnmatch(
        to_addr_norm, _normalize(condition.to_glob)
    ):
        return False

    # Contains
    if condition.from_contains is not None:
        needles = (
            [condition.from_contains]
            if isinstance(condition.from_contains, str)
            else condition.from_contains
        )
        if not any(_normalize(n) in from_addr_norm for n in needles):
            return False

    if condition.to_contains is not None:
        needles = (
            [condition.to_contains]
            if isinstance(condition.to_contains, str)
            else condition.to_contains
        )
        if not any(_normalize(n) in to_addr_norm for n in needles):
            return False

    if condition.subject_contains is not None:
        needles = (
            [condition.subject_contains]
            if isinstance(condition.subject_contains, str)
            else condition.subject_contains
        )
        if not any(_normalize(n) in subject_norm for n in needles):
            return False

    if condition.body_contains is not None:
        if body_norm is None:
            return False
        needles = (
            [condition.body_contains]
            if isinstance(condition.body_contains, str)
            else condition.body_contains
        )
        if not any(_normalize(n) in body_norm for n in needles):
            return False

    # Regex
    if condition.subject_regex is not None and not _match_regex(
        condition.subject_regex, email.subject
    ):
        return False

    # Attachment
    if (
        condition.has_attachment is not None
        and email.has_attachment != condition.has_attachment
    ):
        return False

    # Intent matching (requires LLM client)
    if condition.subject_intent is not None:
        if llm_client is None:
            # Can't evaluate intent without LLM client - fail closed
            return False

        intent_config = condition.subject_intent
        if isinstance(intent_config, str):
            # Reference to named intent
            if intents is None or intent_config not in intents:
                return False
            cfg = intents[intent_config]
            prompt = cfg["prompt"]
            model = cfg["model"]
        else:
            # Inline intent config (IntentConfig instance)
            prompt = intent_config.prompt
            model = intent_config.model

        if not llm_client.check_intent(email.subject, prompt, model):
            return False

    return True


def evaluate_chain(
    config: RulesConfig,
    chain_name: str,
    email: Email,
    llm_client: LLMClient | None = None,
    _visited: set[str] | None = None,
    _intents: dict | None = None,
) -> EvaluationResult:
    """Evaluate a chain of rules against an email.

    Args:
        config: The rules configuration.
        chain_name: Name of the chain to evaluate.
        email: The email to evaluate.
        llm_client: Optional LLM client for intent/classification.
        _visited: Set of visited chains (for cycle detection).
        _intents: Pre-computed intents dict (for efficiency in recursive calls).

    Returns:
        EvaluationResult with match details and action.
    """
    if _visited is None:
        _visited = set()

    # Cycle detection
    if chain_name in _visited:
        return EvaluationResult(matched=False)
    _visited.add(chain_name)

    chain = config.chains.get(chain_name)
    if chain is None:
        return EvaluationResult(matched=False)

    # Use pre-computed intents or compute once
    intents = _intents or {k: v.model_dump() for k, v in config.intents.items()}

    for i, rule in enumerate(chain):
        # Check if rule matches
        if not matches_condition(rule.match, email, llm_client, intents):
            continue

        # Rule matched!

        # Handle jump
        if rule.jump:
            result = evaluate_chain(
                config, rule.jump, email, llm_client, _visited.copy(), intents
            )
            if result.matched:
                return result
            # Jump chain didn't match - continue to next rule
            continue

        # Handle LLM classification
        if rule.llm and rule.routes:
            if llm_client is None:
                log.warning(
                    "LLM rule found but no LLM client provided, skipping rule",
                    rule_index=i,
                    chain=chain_name,
                )
                continue

            category, confidence, reasoning = llm_client.classify(
                from_addr=email.from_addr,
                subject=email.subject,
                body=email.body or "",
                model=rule.llm.model,
                prompt_version=rule.llm.prompt_version,
            )

            if category in rule.routes:
                action = _apply_label_prefix(rule.routes[category], config.label_prefix)
                return EvaluationResult(
                    matched=True,
                    chain=chain_name,
                    rule_index=i,
                    action=action,
                    llm_category=category,
                    llm_confidence=confidence,
                    llm_reasoning=reasoning,
                )
            # Category not in routes - continue
            continue

        # Handle action
        if rule.action:
            action = _apply_label_prefix(rule.action, config.label_prefix)
            return EvaluationResult(
                matched=True,
                chain=chain_name,
                rule_index=i,
                action=action,
            )

    # No rules matched
    return EvaluationResult(matched=False)


def evaluate(
    config: RulesConfig,
    email: Email,
    llm_client: LLMClient | None = None,
) -> EvaluationResult:
    """Evaluate rules against an email, starting from 'main' chain.

    Args:
        config: The rules configuration.
        email: The email to evaluate.
        llm_client: Optional LLM client for intent/classification.

    Returns:
        EvaluationResult with match details and action.
    """
    # Pre-compute intents once for all chain evaluations
    intents = {k: v.model_dump() for k, v in config.intents.items()}
    return evaluate_chain(config, "main", email, llm_client, None, intents)
