"""Match condition evaluation for rules engine."""

import fnmatch
import re
from typing import Protocol

import structlog

from triage.engine.rules import format_classification_prompt
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
        """Check if subject matches an intent (subject only).

        Args:
            subject: Email subject being checked (for logging/context).
            prompt: The fully formatted prompt string to send to the LLM.
            model: Model name to use.

        Returns:
            True if the intent matches, False otherwise.
        """
        ...

    def check_email_intent(
        self,
        from_addr: str,
        subject: str,
        body: str | None,
        prompt: str,
        model: str,
    ) -> bool:
        """Check if full email matches an intent.

        Args:
            from_addr: Email sender address.
            subject: Email subject.
            body: Email body (may be None).
            prompt: Prompt template with {from_addr}, {subject}, {body_preview}.
            model: Model name to use.

        Returns:
            True if the intent matches, False otherwise.
        """
        ...

    def categorize_email(
        self,
        from_addr: str,
        subject: str,
        body: str | None,
        prompt: str,
        model: str,
        categories: list[str],
    ) -> str | None:
        """Categorize an email into one of the predefined categories.

        Args:
            from_addr: Email sender address.
            subject: Email subject.
            body: Email body (may be None).
            prompt: Prompt template with {from_addr}, {subject},
                {body_preview}, {categories}.
            model: Model name to use.
            categories: List of valid category names.

        Returns:
            The matched category name, or None if no valid category matched.
        """
        ...

    def classify(self, prompt: str, model: str) -> tuple[str, float, str]:
        """Classify an email.

        Args:
            prompt: Fully formatted classification prompt.
            model: Model name to use.

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


# URL regex pattern - matches http/https URLs
_URL_PATTERN = re.compile(
    r'https?://[^\s<>"\')\]]+',
    re.IGNORECASE,
)


def _is_mostly_links(body: str | None, threshold: float = 0.5) -> bool:
    """Check if the body is mostly URLs.

    Args:
        body: Email body text.
        threshold: Fraction of non-whitespace content that must be URLs (default 0.5).

    Returns:
        True if URLs make up more than threshold of the non-whitespace content.
    """
    if not body:
        return False

    # Strip and get non-whitespace content length
    body_stripped = body.strip()
    if not body_stripped:
        return False

    # Calculate content length without creating intermediate string (memory efficient)
    whitespace_count = sum(1 for _ in re.finditer(r"\s", body_stripped))
    content_length = len(body_stripped) - whitespace_count
    if content_length == 0:
        return False

    # Find all URLs and calculate their total length (without whitespace)
    urls = _URL_PATTERN.findall(body_stripped)
    if not urls:
        return False

    # Strip trailing punctuation that's unlikely to be part of URLs
    # Note: ) is already excluded by the regex pattern
    url_length = sum(len(url.rstrip(".,;:")) for url in urls)

    # Check if URLs make up more than threshold of content
    return (url_length / content_length) >= threshold


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
    email_categories: dict | None = None,
    trace: list[str] | None = None,
) -> bool:
    """Check if an email matches a condition.

    Args:
        condition: The match condition to evaluate.
        email: The email to match against.
        llm_client: Optional LLM client for intent matching.
        intents: Intent configurations for intent matching.
        email_categories: Email category configurations for categorization.
        trace: Optional list to append decision trace messages to.

    Returns:
        True if all specified conditions match, False otherwise.
        Empty condition (no fields set) matches everything.
        Within each field, lists are OR (any value matches).
    """
    # Pre-normalize email fields for efficiency
    from_addr_norm = _normalize(email.from_addr)
    to_addrs_norm = [_normalize(a) for a in email.to_addrs]
    subject_norm = _normalize(email.subject)
    body_norm = _normalize(email.body) if email.body else None

    def _trace(msg: str) -> None:
        if trace is not None:
            trace.append(msg)

    # Check each condition type - all specified must match (AND)
    # Within each field, lists are OR (any value matches)

    # Exact from match (any in list)
    from_addrs = _to_list(condition.from_addr)
    if from_addrs:
        matched = any(from_addr_norm == _normalize(a) for a in from_addrs)
        _trace(f"from_addr check: {from_addr_norm} in {from_addrs} = {matched}")
        if not matched:
            return False

    # Exact to match - use set intersection for O(N+M) instead of O(N*M)
    cond_to_addrs = _to_list(condition.to_addr)
    if cond_to_addrs:
        cond_addrs_set = {_normalize(addr) for addr in cond_to_addrs}
        matched = bool(set(to_addrs_norm).intersection(cond_addrs_set))
        _trace(f"to_addr check: {to_addrs_norm} intersects {cond_to_addrs} = {matched}")
        if not matched:
            return False

    # Glob patterns (any in list) - pre-normalize globs
    from_globs = _to_list(condition.from_glob)
    if from_globs:
        normalized_globs = [_normalize(g) for g in from_globs]
        if not any(fnmatch.fnmatch(from_addr_norm, g) for g in normalized_globs):
            return False

    # To glob (any pattern matches any recipient) - pre-normalize globs
    to_globs = _to_list(condition.to_glob)
    if to_globs:
        normalized_globs = [_normalize(g) for g in to_globs]
        if not any(
            fnmatch.fnmatch(email_to, g)
            for email_to in to_addrs_norm
            for g in normalized_globs
        ):
            return False

    # Contains (any needle in list) - pre-normalize needles
    from_contains = _to_list(condition.from_contains)
    if from_contains:
        norm_contains = [_normalize(n) for n in from_contains]
        if not any(n in from_addr_norm for n in norm_contains):
            return False

    # To contains (any needle in any recipient) - pre-normalize needles
    to_contains = _to_list(condition.to_contains)
    if to_contains:
        norm_contains = [_normalize(n) for n in to_contains]
        if not any(n in email_to for email_to in to_addrs_norm for n in norm_contains):
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

    # Header matching (glob patterns) - all specified headers must match
    if condition.header:
        for header_name, pattern in condition.header.items():
            header_value = email.headers.get(header_name.lower(), "")
            # Missing header (empty value) never matches
            if not header_value or not _match_glob(pattern, header_value):
                _trace(f"header[{header_name}]: '{header_value}' !~ '{pattern}'")
                return False

    # Header contains (substring) - all specified headers must contain substring
    if condition.header_contains:
        for header_name, substring in condition.header_contains.items():
            header_value = email.headers.get(header_name.lower(), "")
            if _normalize(substring) not in _normalize(header_value):
                _trace(f"header_contains[{header_name}]: no match")
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

    # Body is mostly links
    if condition.body_is_mostly_links is not None:
        is_mostly = _is_mostly_links(email.body)
        _trace(f"body_is_mostly_links: {is_mostly} == {condition.body_is_mostly_links}")
        if is_mostly != condition.body_is_mostly_links:
            return False

    # Intent matching (requires LLM client)
    if condition.subject_intent is not None:
        if llm_client is None:
            # Can't evaluate intent without LLM client - fail closed
            _trace("subject_intent: no LLM client, failing")
            return False

        intent_config = condition.subject_intent
        if isinstance(intent_config, str):
            # Reference to named intent
            if intents is None or intent_config not in intents:
                _trace(f"subject_intent: intent '{intent_config}' not found")
                return False
            cfg = intents[intent_config]
            prompt = cfg["prompt"]
            model = cfg["model"]
        else:
            # Inline intent config (IntentConfig instance)
            prompt = intent_config.prompt
            model = intent_config.model

        # Format the prompt with subject
        formatted_prompt = prompt.format(subject=email.subject)
        result = llm_client.check_intent(email.subject, formatted_prompt, model)
        _trace(
            f"subject_intent '{intent_config}': subject='{email.subject}' -> {result}"
        )
        if not result:
            return False

    # Email intent matching (full email context, requires LLM client)
    if condition.email_intent is not None:
        if llm_client is None:
            return False

        intent_config = condition.email_intent
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

        if not llm_client.check_email_intent(
            email.from_addr, email.subject, email.body, prompt, model
        ):
            return False

    # Email category matching (multi-class classifier, requires LLM client)
    if condition.email_category is not None:
        if llm_client is None:
            return False

        # Parse "category_name/expected_value" format
        if "/" not in condition.email_category:
            log.warning(
                "Invalid email_category format, expected 'category_name/value'",
                email_category=condition.email_category,
            )
            return False

        category_name, expected_value = condition.email_category.split("/", 1)

        if not category_name or not expected_value:
            log.warning(
                "Invalid email_category format, name or value is empty",
                email_category=condition.email_category,
            )
            return False

        if email_categories is None or category_name not in email_categories:
            log.warning(
                "Email category not found in config",
                category_name=category_name,
            )
            return False

        cfg = email_categories[category_name]
        actual_category = llm_client.categorize_email(
            email.from_addr,
            email.subject,
            email.body,
            cfg["prompt"],
            cfg["model"],
            cfg["categories"],
        )

        if actual_category != expected_value:
            return False

    # Compound conditions
    # any_of: at least one sub-condition must match (OR)
    if condition.any_of is not None:
        any_matched = any(
            matches_condition(sub, email, llm_client, intents, email_categories, trace)
            for sub in condition.any_of
        )
        _trace(f"any_of: {any_matched}")
        if not any_matched:
            return False

    # all_of: all sub-conditions must match (AND)
    if condition.all_of is not None:
        all_matched = all(
            matches_condition(sub, email, llm_client, intents, email_categories, trace)
            for sub in condition.all_of
        )
        _trace(f"all_of: {all_matched}")
        return all_matched

    return True


def evaluate_chain(
    config: RulesConfig,
    chain_name: str,
    email: Email,
    llm_client: LLMClient | None = None,
    _visited: set[str] | None = None,
    _intents: dict | None = None,
    _email_categories: dict | None = None,
    _trace: list[str] | None = None,
) -> EvaluationResult:
    """Evaluate a chain of rules against an email.

    Args:
        config: The rules configuration.
        chain_name: Name of the chain to evaluate.
        email: The email to evaluate.
        llm_client: Optional LLM client for intent/classification.
        _visited: Set of visited chains (for cycle detection).
        _intents: Pre-computed intents dict (for efficiency in recursive calls).
        _email_categories: Pre-computed email_categories dict.
        _trace: Accumulated decision trace.

    Returns:
        EvaluationResult with match details and action.
    """
    if _visited is None:
        _visited = set()
    if _trace is None:
        _trace = []

    # Cycle detection
    if chain_name in _visited:
        _trace.append(f"chain '{chain_name}': cycle detected, skipping")
        return EvaluationResult(matched=False, trace=_trace)
    _visited.add(chain_name)

    chain = config.chains.get(chain_name)
    if chain is None:
        _trace.append(f"chain '{chain_name}': not found")
        return EvaluationResult(matched=False, trace=_trace)

    _trace.append(f"chain '{chain_name}': evaluating {len(chain)} rules")

    # Use pre-computed dicts or compute once
    intents = _intents or {k: v.model_dump() for k, v in config.intents.items()}
    email_categories = _email_categories or {
        k: v.model_dump() for k, v in config.email_categories.items()
    }

    for i, rule in enumerate(chain):
        rule_trace: list[str] = []
        # Check if rule matches
        matched = matches_condition(
            rule.match, email, llm_client, intents, email_categories, rule_trace
        )
        _trace.append(f"  rule[{i}]: {matched}")
        _trace.extend(f"    {t}" for t in rule_trace)

        if not matched:
            continue

        # Rule matched!

        # Handle return_to_parent - exit this chain and continue in parent
        if rule.return_to_parent:
            _trace.append(f"  rule[{i}]: chain '{chain_name}' returning to parent")
            return EvaluationResult(matched=False, returned=True, trace=_trace)

        # Handle jump
        if rule.jump:
            _trace.append(f"  rule[{i}]: jumping to chain '{rule.jump}'")
            result = evaluate_chain(
                config,
                rule.jump,
                email,
                llm_client,
                _visited.copy(),
                intents,
                email_categories,
                _trace,
            )
            if result.matched:
                return result
            if result.returned:
                # Sub-chain returned, continue matching in this chain
                _trace.append(
                    f"  rule[{i}]: jump chain '{rule.jump}' returned, continuing"
                )
                continue
            # Jump chain didn't match - continue to next rule
            _trace.append(
                f"  rule[{i}]: jump chain '{rule.jump}' didn't match, continuing"
            )
            continue

        # Handle LLM classification
        if rule.llm and rule.routes:
            if llm_client is None:
                log.warning(
                    "LLM rule found but no LLM client provided, skipping rule",
                    rule_index=i,
                    chain=chain_name,
                )
                _trace.append(f"  rule[{i}]: LLM rule but no client, skipping")
                continue

            # Format prompt from config
            prompt, model = format_classification_prompt(
                config,
                rule.llm.prompt_version,
                email.from_addr,
                email.subject,
                email.body or "",
            )
            # Override model if specified in rule
            if rule.llm.model:
                model = rule.llm.model

            category, confidence, reasoning = llm_client.classify(prompt, model)
            _trace.append(
                f"  rule[{i}]: LLM classified as '{category}' "
                f"(confidence={confidence:.2f})"
            )

            if category in rule.routes:
                action = _apply_label_prefix(rule.routes[category], config.label_prefix)
                _trace.append(f"  rule[{i}]: routed to action {action}")
                return EvaluationResult(
                    matched=True,
                    chain=chain_name,
                    rule_index=i,
                    action=action,
                    llm_category=category,
                    llm_confidence=confidence,
                    llm_reasoning=reasoning,
                    trace=_trace,
                )
            # Category not in routes - continue
            _trace.append(
                f"  rule[{i}]: category '{category}' not in routes, continuing"
            )
            continue

        # Handle action
        if rule.action:
            action = _apply_label_prefix(rule.action, config.label_prefix)
            _trace.append(f"  rule[{i}]: matched with action {action}")
            return EvaluationResult(
                matched=True,
                chain=chain_name,
                rule_index=i,
                action=action,
                trace=_trace,
            )

    # No rules matched
    _trace.append(f"chain '{chain_name}': no rules matched")
    return EvaluationResult(matched=False, trace=_trace)


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
    # Pre-compute dicts once for all chain evaluations
    intents = {k: v.model_dump() for k, v in config.intents.items()}
    email_categories = {k: v.model_dump() for k, v in config.email_categories.items()}
    return evaluate_chain(
        config, "main", email, llm_client, None, intents, email_categories
    )
