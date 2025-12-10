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
            subject: Email subject being checked (for logging/context).
            prompt: The fully formatted prompt string to send to the LLM.
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


def _match_regex(pattern: str, value: str) -> bool:
    """Match a regex pattern against a value."""
    try:
        return bool(re.search(pattern, value, re.IGNORECASE))
    except re.error:
        log.error(
            "Invalid regex pattern encountered during matching",
            pattern=pattern,
            exc_info=True,
        )
        return False


def _to_list(value: list[str] | str | None) -> list[str]:
    """Convert a single string or list to a list."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return value


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
        Within each field, lists are OR (any value matches).
    """
    # Pre-normalize email fields for efficiency
    from_addr_norm = _normalize(email.from_addr)
    to_addr_norm = _normalize(email.to_addr)
    subject_norm = _normalize(email.subject)
    body_norm = _normalize(email.body) if email.body else None

    # Check each condition type - all specified must match (AND)
    # Within each field, lists are OR (any value matches)

    # Exact from match (any in list)
    from_addrs = _to_list(condition.from_addr)
    if from_addrs and not any(from_addr_norm == _normalize(a) for a in from_addrs):
        return False

    # Exact to match (any in list)
    to_addrs = _to_list(condition.to_addr)
    if to_addrs and not any(to_addr_norm == _normalize(a) for a in to_addrs):
        return False

    # Glob patterns (any in list)
    from_globs = _to_list(condition.from_glob)
    if from_globs and not any(
        fnmatch.fnmatch(from_addr_norm, _normalize(g)) for g in from_globs
    ):
        return False

    to_globs = _to_list(condition.to_glob)
    if to_globs and not any(
        fnmatch.fnmatch(to_addr_norm, _normalize(g)) for g in to_globs
    ):
        return False

    # Contains (any needle in list)
    from_contains = _to_list(condition.from_contains)
    if from_contains and not any(
        _normalize(n) in from_addr_norm for n in from_contains
    ):
        return False

    to_contains = _to_list(condition.to_contains)
    if to_contains and not any(_normalize(n) in to_addr_norm for n in to_contains):
        return False

    subject_contains = _to_list(condition.subject_contains)
    if subject_contains and not any(
        _normalize(n) in subject_norm for n in subject_contains
    ):
        return False

    body_contains = _to_list(condition.body_contains)
    if body_contains:
        if body_norm is None:
            return False
        if not any(_normalize(n) in body_norm for n in body_contains):
            return False

    # Regex (any pattern in list)
    subject_regexes = _to_list(condition.subject_regex)
    if subject_regexes and not any(
        _match_regex(pattern, email.subject) for pattern in subject_regexes
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

    # Compound conditions
    # any_of: at least one sub-condition must match (OR)
    if condition.any_of is not None and not any(
        matches_condition(sub, email, llm_client, intents) for sub in condition.any_of
    ):
        return False

    # all_of: all sub-conditions must match (AND)
    if condition.all_of is not None:
        return all(
            matches_condition(sub, email, llm_client, intents)
            for sub in condition.all_of
        )

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
