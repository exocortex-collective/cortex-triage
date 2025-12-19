"""Match condition evaluation for rules engine."""

import fnmatch
import re
import time
from datetime import UTC, datetime
from functools import lru_cache
from typing import Protocol

import structlog

from triage.engine.rules import format_classification_prompt
from triage.engine.variables import (
    TEMPLATE_PATTERN,
    VariableContext,
    VariableError,
    resolve_variables,
)
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

    def extract_value(
        self,
        from_addr: str,
        subject: str,
        body: str | None,
        prompt: str,
        model: str,
    ) -> str | None:
        """Extract a single value from email content.

        Used for standalone variable extraction with LLM.

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

    def classify_with_extraction(
        self,
        prompt: str,
        model: str,
        extract_fields: list[str] | None = None,
    ) -> tuple[str, float, str, dict[str, str]]:
        """Classify email and optionally extract fields.

        When extract_fields is specified, the LLM response should include
        an "extracted" dict with the requested field values.

        Args:
            prompt: Fully formatted classification prompt
                (includes extraction instructions).
            model: Model name to use.
            extract_fields: Optional list of field names to extract.

        Returns:
            Tuple of (category, confidence, reasoning, extracted_dict).
            extracted_dict will be empty if extract_fields is None.
        """
        ...


@lru_cache(maxsize=256)
def _compile_regex(pattern: str) -> re.Pattern[str]:
    """Compile and cache a regex pattern.

    Uses LRU cache to avoid recompiling the same patterns repeatedly.
    Patterns are compiled with IGNORECASE flag.
    """
    return re.compile(pattern, re.IGNORECASE)


# Duration pattern for relative dates (e.g., "7d", "2w", "1m", "1y")
_DURATION_PATTERN = re.compile(r"^(\d+)([dwmy])$", re.IGNORECASE)


def _parse_date_to_ms(date_value: str | int) -> int | None:
    """Parse a date value to milliseconds since epoch.

    Args:
        date_value: Either an ISO date string (YYYY-MM-DD) or a timestamp in ms.

    Returns:
        Timestamp in milliseconds, or None if parsing fails.
    """
    if isinstance(date_value, int):
        return date_value

    # Try parsing ISO date format
    try:
        dt = datetime.fromisoformat(date_value.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1000)
    except ValueError:
        pass

    # Try parsing date-only format
    try:
        dt = datetime.strptime(date_value, "%Y-%m-%d").replace(tzinfo=UTC)
        return int(dt.timestamp() * 1000)
    except ValueError:
        pass

    log.warning("Failed to parse date", date_value=date_value)
    return None


def _parse_duration_to_ms(duration: str) -> int | None:
    """Parse a duration string to milliseconds.

    Supported formats:
    - "7d" = 7 days
    - "2w" = 2 weeks
    - "1m" = 1 month (30 days)
    - "1y" = 1 year (365 days)

    Args:
        duration: Duration string like "7d", "2w", "1m", "1y".

    Returns:
        Duration in milliseconds, or None if parsing fails.
    """
    match = _DURATION_PATTERN.match(duration)
    if not match:
        log.warning("Invalid duration format", duration=duration)
        return None

    amount = int(match.group(1))
    unit = match.group(2).lower()

    # Convert to milliseconds
    ms_per_day = 24 * 60 * 60 * 1000
    if unit == "d":
        return amount * ms_per_day
    elif unit == "w":
        return amount * 7 * ms_per_day
    elif unit == "m":
        return amount * 30 * ms_per_day  # Approximate month
    elif unit == "y":
        return amount * 365 * ms_per_day  # Approximate year

    return None


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
    """Match a regex pattern against a value (case-insensitive).

    Uses cached compiled patterns for performance.
    """
    try:
        compiled = _compile_regex(pattern)
        return bool(compiled.search(value))
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


def _render_label(
    label: str,
    prefix: str,
    variables: VariableContext | None = None,
) -> str:
    """Render label template with variables and apply prefix.

    Args:
        label: Label string, possibly with {{variable}} placeholders.
        prefix: Label prefix to prepend (e.g., "Cortex").
        variables: Optional variable context for substitution.

    Returns:
        Rendered label with variables substituted and prefix applied.
    """
    # Substitute {{variable}} placeholders
    if variables:

        def replace(match: re.Match[str]) -> str:
            var_name = match.group(1)
            return variables.values.get(var_name, match.group(0))

        label = TEMPLATE_PATTERN.sub(replace, label)

    # Apply prefix
    if prefix:
        label = f"{prefix}/{label}"

    return label


def _render_action(
    action: Action,
    prefix: str,
    variables: VariableContext | None = None,
) -> Action:
    """Apply label rendering to action.

    Args:
        action: Action with possibly templated label.
        prefix: Label prefix to prepend.
        variables: Optional variable context for substitution.

    Returns:
        New action with rendered label.
    """
    if action.label:
        rendered = _render_label(action.label, prefix, variables)
        return action.model_copy(update={"label": rendered})
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

    def _result(matched: bool) -> bool:
        """Apply negation to the match result."""
        if condition.negate:
            _trace(f"negate: inverting {matched} -> {not matched}")
            return not matched
        return matched

    # Check each condition type - all specified must match (AND)
    # Within each field, lists are OR (any value matches)

    # Exact from match (any in list)
    from_addrs = _to_list(condition.from_addr)
    if from_addrs:
        matched = any(from_addr_norm == _normalize(a) for a in from_addrs)
        _trace(f"from_addr check: {from_addr_norm} in {from_addrs} = {matched}")
        if not matched:
            return _result(False)

    # Exact to match - use set intersection for O(N+M) instead of O(N*M)
    cond_to_addrs = _to_list(condition.to_addr)
    if cond_to_addrs:
        cond_addrs_set = {_normalize(addr) for addr in cond_to_addrs}
        matched = bool(set(to_addrs_norm).intersection(cond_addrs_set))
        _trace(f"to_addr check: {to_addrs_norm} intersects {cond_to_addrs} = {matched}")
        if not matched:
            return _result(False)

    # Glob patterns (any in list) - pre-normalize globs
    from_globs = _to_list(condition.from_glob)
    if from_globs:
        normalized_globs = [_normalize(g) for g in from_globs]
        if not any(fnmatch.fnmatch(from_addr_norm, g) for g in normalized_globs):
            return _result(False)

    # To glob (any pattern matches any recipient) - pre-normalize globs
    to_globs = _to_list(condition.to_glob)
    if to_globs:
        normalized_globs = [_normalize(g) for g in to_globs]
        if not any(
            fnmatch.fnmatch(email_to, g)
            for email_to in to_addrs_norm
            for g in normalized_globs
        ):
            return _result(False)

    # Contains (any needle in list) - pre-normalize needles
    from_contains = _to_list(condition.from_contains)
    if from_contains:
        norm_contains = [_normalize(n) for n in from_contains]
        if not any(n in from_addr_norm for n in norm_contains):
            return _result(False)

    # To contains (any needle in any recipient) - pre-normalize needles
    to_contains = _to_list(condition.to_contains)
    if to_contains:
        norm_contains = [_normalize(n) for n in to_contains]
        if not any(n in email_to for email_to in to_addrs_norm for n in norm_contains):
            return _result(False)

    # From regex (any pattern in list)
    from_regexes = _to_list(condition.from_regex)
    if from_regexes and not any(
        _match_regex(pattern, email.from_addr) for pattern in from_regexes
    ):
        return _result(False)

    # To regex (any pattern matches any recipient)
    to_regexes = _to_list(condition.to_regex)
    if to_regexes and not any(
        _match_regex(pattern, email_to)
        for email_to in email.to_addrs
        for pattern in to_regexes
    ):
        return _result(False)

    # Subject exact match (any in list)
    subjects = _to_list(condition.subject)
    if subjects and not any(subject_norm == _normalize(s) for s in subjects):
        return _result(False)

    # Subject glob (any in list)
    subject_globs = _to_list(condition.subject_glob)
    if subject_globs and not any(
        fnmatch.fnmatch(subject_norm, _normalize(g)) for g in subject_globs
    ):
        return _result(False)

    subject_contains = _to_list(condition.subject_contains)
    if subject_contains and not any(
        _normalize(n) in subject_norm for n in subject_contains
    ):
        return _result(False)

    # Body glob (any in list)
    body_globs = _to_list(condition.body_glob)
    if body_globs:
        if body_norm is None:
            return _result(False)
        if not any(fnmatch.fnmatch(body_norm, _normalize(g)) for g in body_globs):
            return _result(False)

    body_contains = _to_list(condition.body_contains)
    if body_contains:
        if body_norm is None:
            return _result(False)
        if not any(_normalize(n) in body_norm for n in body_contains):
            return _result(False)

    # Body regex (any pattern in list)
    body_regexes = _to_list(condition.body_regex)
    if body_regexes:
        if email.body is None:
            return _result(False)
        if not any(_match_regex(pattern, email.body) for pattern in body_regexes):
            return _result(False)

    # Header matching (glob patterns) - all specified headers must match
    if condition.header:
        for header_name, pattern in condition.header.items():
            header_value = email.headers.get(header_name.lower(), "")
            # Missing header (empty value) never matches
            if not header_value or not _match_glob(pattern, header_value):
                _trace(f"header[{header_name}]: '{header_value}' !~ '{pattern}'")
                return _result(False)

    # Header contains (substring) - all specified headers must contain substring
    if condition.header_contains:
        for header_name, substring in condition.header_contains.items():
            header_value = email.headers.get(header_name.lower(), "")
            # Empty substring would match everything - treat as no match
            if not substring or _normalize(substring) not in _normalize(header_value):
                _trace(f"header_contains[{header_name}]: no match")
                return _result(False)

    # Header regex - all specified headers must match regex
    if condition.header_regex:
        for header_name, pattern in condition.header_regex.items():
            header_value = email.headers.get(header_name.lower(), "")
            # Missing header (empty value) never matches
            if not header_value or not _match_regex(pattern, header_value):
                _trace(f"header_regex[{header_name}]: '{header_value}' !~ '{pattern}'")
                return _result(False)

    # Header exists - check if header(s) are present
    header_exists = _to_list(condition.header_exists)
    if header_exists:
        for header_name in header_exists:
            if header_name.lower() not in email.headers:
                _trace(f"header_exists[{header_name}]: not present")
                return _result(False)

    # Header name glob - match both header name pattern and value pattern
    if condition.header_name_glob:
        for name_pattern, value_pattern in condition.header_name_glob.items():
            matched = False
            for header_name, header_value in email.headers.items():
                if _match_glob(name_pattern, header_name) and _match_glob(
                    value_pattern, header_value
                ):
                    matched = True
                    break
            if not matched:
                _trace(f"header_name_glob[{name_pattern}]: no match")
                return _result(False)

    # Header name regex - match both header name pattern and value pattern
    if condition.header_name_regex:
        for name_pattern, value_pattern in condition.header_name_regex.items():
            matched = False
            for header_name, header_value in email.headers.items():
                if _match_regex(name_pattern, header_name) and _match_regex(
                    value_pattern, header_value
                ):
                    matched = True
                    break
            if not matched:
                _trace(f"header_name_regex[{name_pattern}]: no match")
                return _result(False)

    # List-ID aliases (shortcuts for header matching on List-ID)
    list_id_value = email.headers.get("list-id", "")

    list_ids = _to_list(condition.list_id)
    if list_ids:
        if not list_id_value:
            return _result(False)
        list_id_norm = _normalize(list_id_value)
        if not any(list_id_norm == _normalize(lid) for lid in list_ids):
            return _result(False)

    list_id_globs = _to_list(condition.list_id_glob)
    if list_id_globs:
        if not list_id_value:
            return _result(False)
        list_id_norm = _normalize(list_id_value)
        if not any(fnmatch.fnmatch(list_id_norm, _normalize(g)) for g in list_id_globs):
            return _result(False)

    list_id_contains = _to_list(condition.list_id_contains)
    if list_id_contains:
        if not list_id_value:
            return _result(False)
        list_id_norm = _normalize(list_id_value)
        if not any(_normalize(s) in list_id_norm for s in list_id_contains):
            return _result(False)

    list_id_regexes = _to_list(condition.list_id_regex)
    if list_id_regexes:
        if not list_id_value:
            return _result(False)
        if not any(_match_regex(pattern, list_id_value) for pattern in list_id_regexes):
            return _result(False)

    # Reply-To aliases (shortcuts for header matching on Reply-To)
    reply_to_value = email.headers.get("reply-to", "")

    reply_tos = _to_list(condition.reply_to)
    if reply_tos:
        if not reply_to_value:
            return _result(False)
        reply_to_norm = _normalize(reply_to_value)
        if not any(reply_to_norm == _normalize(rt) for rt in reply_tos):
            return _result(False)

    reply_to_globs = _to_list(condition.reply_to_glob)
    if reply_to_globs:
        if not reply_to_value:
            return _result(False)
        reply_to_norm = _normalize(reply_to_value)
        if not any(
            fnmatch.fnmatch(reply_to_norm, _normalize(g)) for g in reply_to_globs
        ):
            return _result(False)

    reply_to_contains = _to_list(condition.reply_to_contains)
    if reply_to_contains:
        if not reply_to_value:
            return _result(False)
        reply_to_norm = _normalize(reply_to_value)
        if not any(_normalize(s) in reply_to_norm for s in reply_to_contains):
            return _result(False)

    reply_to_regexes = _to_list(condition.reply_to_regex)
    if reply_to_regexes:
        if not reply_to_value:
            return _result(False)
        if not any(
            _match_regex(pattern, reply_to_value) for pattern in reply_to_regexes
        ):
            return _result(False)

    # CC matching (from emails_parsed.cc_addrs)
    cc_addrs_norm = [_normalize(a) for a in email.cc_addrs]

    cc_addrs = _to_list(condition.cc)
    if cc_addrs:
        if not cc_addrs_norm:
            return _result(False)
        cond_cc_set = {_normalize(addr) for addr in cc_addrs}
        if not set(cc_addrs_norm).intersection(cond_cc_set):
            return _result(False)

    cc_globs = _to_list(condition.cc_glob)
    if cc_globs:
        if not cc_addrs_norm:
            return _result(False)
        normalized_globs = [_normalize(g) for g in cc_globs]
        if not any(
            fnmatch.fnmatch(cc_addr, g)
            for cc_addr in cc_addrs_norm
            for g in normalized_globs
        ):
            return _result(False)

    cc_contains = _to_list(condition.cc_contains)
    if cc_contains:
        if not cc_addrs_norm:
            return _result(False)
        norm_contains = [_normalize(n) for n in cc_contains]
        if not any(n in cc_addr for cc_addr in cc_addrs_norm for n in norm_contains):
            return _result(False)

    cc_regexes = _to_list(condition.cc_regex)
    if cc_regexes:
        if not email.cc_addrs:
            return _result(False)
        if not any(
            _match_regex(pattern, cc_addr)
            for cc_addr in email.cc_addrs
            for pattern in cc_regexes
        ):
            return _result(False)

    # BCC matching (from emails_parsed.bcc_addrs)
    bcc_addrs_norm = [_normalize(a) for a in email.bcc_addrs]

    bcc_addrs = _to_list(condition.bcc)
    if bcc_addrs:
        if not bcc_addrs_norm:
            return _result(False)
        cond_bcc_set = {_normalize(addr) for addr in bcc_addrs}
        if not set(bcc_addrs_norm).intersection(cond_bcc_set):
            return _result(False)

    bcc_globs = _to_list(condition.bcc_glob)
    if bcc_globs:
        if not bcc_addrs_norm:
            return _result(False)
        normalized_globs = [_normalize(g) for g in bcc_globs]
        if not any(
            fnmatch.fnmatch(bcc_addr, g)
            for bcc_addr in bcc_addrs_norm
            for g in normalized_globs
        ):
            return _result(False)

    bcc_contains = _to_list(condition.bcc_contains)
    if bcc_contains:
        if not bcc_addrs_norm:
            return _result(False)
        norm_contains = [_normalize(n) for n in bcc_contains]
        if not any(n in bcc_addr for bcc_addr in bcc_addrs_norm for n in norm_contains):
            return _result(False)

    bcc_regexes = _to_list(condition.bcc_regex)
    if bcc_regexes:
        if not email.bcc_addrs:
            return _result(False)
        if not any(
            _match_regex(pattern, bcc_addr)
            for bcc_addr in email.bcc_addrs
            for pattern in bcc_regexes
        ):
            return _result(False)

    # Delivered-To header matching
    deliveredto_value = email.headers.get("delivered-to", "")

    deliveredtos = _to_list(condition.deliveredto)
    if deliveredtos:
        if not deliveredto_value:
            return _result(False)
        deliveredto_norm = _normalize(deliveredto_value)
        if not any(deliveredto_norm == _normalize(dt) for dt in deliveredtos):
            return _result(False)

    deliveredto_globs = _to_list(condition.deliveredto_glob)
    if deliveredto_globs:
        if not deliveredto_value:
            return _result(False)
        deliveredto_norm = _normalize(deliveredto_value)
        if not any(
            fnmatch.fnmatch(deliveredto_norm, _normalize(g)) for g in deliveredto_globs
        ):
            return _result(False)

    deliveredto_contains = _to_list(condition.deliveredto_contains)
    if deliveredto_contains:
        if not deliveredto_value:
            return _result(False)
        deliveredto_norm = _normalize(deliveredto_value)
        if not any(_normalize(s) in deliveredto_norm for s in deliveredto_contains):
            return _result(False)

    deliveredto_regexes = _to_list(condition.deliveredto_regex)
    if deliveredto_regexes:
        if not deliveredto_value:
            return _result(False)
        if not any(
            _match_regex(pattern, deliveredto_value) for pattern in deliveredto_regexes
        ):
            return _result(False)

    # Subject regex (any pattern in list)
    subject_regexes = _to_list(condition.subject_regex)
    if subject_regexes and not any(
        _match_regex(pattern, email.subject) for pattern in subject_regexes
    ):
        return _result(False)

    # Status conditions (based on Gmail label_ids)
    if condition.is_read is not None:
        # is_read = UNREAD not in label_ids
        is_read = "UNREAD" not in email.label_ids
        if is_read != condition.is_read:
            return _result(False)

    if condition.is_starred is not None:
        is_starred = "STARRED" in email.label_ids
        if is_starred != condition.is_starred:
            return _result(False)

    if condition.is_important is not None:
        is_important = "IMPORTANT" in email.label_ids
        if is_important != condition.is_important:
            return _result(False)

    if condition.in_inbox is not None:
        in_inbox = "INBOX" in email.label_ids
        if in_inbox != condition.in_inbox:
            return _result(False)

    # has_label - check for specific label(s)
    has_labels = _to_list(condition.has_label)
    if has_labels:
        label_ids_set = set(email.label_ids)
        if not any(label in label_ids_set for label in has_labels):
            return _result(False)

    # Size conditions
    if condition.size_larger is not None and (
        email.size_estimate is None or email.size_estimate <= condition.size_larger
    ):
        return _result(False)

    if condition.size_smaller is not None and (
        email.size_estimate is None or email.size_estimate >= condition.size_smaller
    ):
        return _result(False)

    # Date conditions
    if condition.date_before is not None:
        if email.internal_date is None:
            return _result(False)
        target_ms = _parse_date_to_ms(condition.date_before)
        if target_ms is None or email.internal_date >= target_ms:
            return _result(False)

    if condition.date_after is not None:
        if email.internal_date is None:
            return _result(False)
        target_ms = _parse_date_to_ms(condition.date_after)
        if target_ms is None or email.internal_date <= target_ms:
            return _result(False)

    if condition.older_than is not None:
        if email.internal_date is None:
            return _result(False)
        duration_ms = _parse_duration_to_ms(condition.older_than)
        if duration_ms is None:
            return _result(False)
        now_ms = int(time.time() * 1000)
        threshold_ms = now_ms - duration_ms
        if email.internal_date >= threshold_ms:
            return _result(False)

    if condition.newer_than is not None:
        if email.internal_date is None:
            return _result(False)
        duration_ms = _parse_duration_to_ms(condition.newer_than)
        if duration_ms is None:
            return _result(False)
        now_ms = int(time.time() * 1000)
        threshold_ms = now_ms - duration_ms
        if email.internal_date <= threshold_ms:
            return _result(False)

    # Attachment conditions
    if (
        condition.has_attachment is not None
        and email.has_attachment != condition.has_attachment
    ):
        return _result(False)

    # Attachment filename matching
    filenames_norm = [_normalize(f) for f in email.attachment_filenames if f]

    att_filenames = _to_list(condition.attachment_filename)
    if att_filenames:
        if not filenames_norm:
            return _result(False)
        cond_set = {_normalize(f) for f in att_filenames}
        if not set(filenames_norm).intersection(cond_set):
            return _result(False)

    att_filename_globs = _to_list(condition.attachment_filename_glob)
    if att_filename_globs:
        if not filenames_norm:
            return _result(False)
        normalized_globs = [_normalize(g) for g in att_filename_globs]
        if not any(
            fnmatch.fnmatch(fn, g) for fn in filenames_norm for g in normalized_globs
        ):
            return _result(False)

    att_filename_contains = _to_list(condition.attachment_filename_contains)
    if att_filename_contains:
        if not filenames_norm:
            return _result(False)
        norm_contains = [_normalize(c) for c in att_filename_contains]
        if not any(c in fn for fn in filenames_norm for c in norm_contains):
            return _result(False)

    att_filename_regexes = _to_list(condition.attachment_filename_regex)
    if att_filename_regexes:
        if not email.attachment_filenames:
            return _result(False)
        if not any(
            _match_regex(pattern, fn)
            for fn in email.attachment_filenames
            if fn
            for pattern in att_filename_regexes
        ):
            return _result(False)

    # Attachment MIME type matching
    mimes_norm = [_normalize(m) for m in email.attachment_mime_types if m]

    att_mimes = _to_list(condition.attachment_mime)
    if att_mimes:
        if not mimes_norm:
            return _result(False)
        cond_set = {_normalize(m) for m in att_mimes}
        if not set(mimes_norm).intersection(cond_set):
            return _result(False)

    att_mime_globs = _to_list(condition.attachment_mime_glob)
    if att_mime_globs:
        if not mimes_norm:
            return _result(False)
        normalized_globs = [_normalize(g) for g in att_mime_globs]
        if not any(
            fnmatch.fnmatch(mime, g) for mime in mimes_norm for g in normalized_globs
        ):
            return _result(False)

    att_mime_contains = _to_list(condition.attachment_mime_contains)
    if att_mime_contains:
        if not mimes_norm:
            return _result(False)
        norm_contains = [_normalize(c) for c in att_mime_contains]
        if not any(c in mime for mime in mimes_norm for c in norm_contains):
            return _result(False)

    # Convenience shortcuts for common attachment types
    if condition.has_calendar_invite is not None:
        has_ics = any(
            fn.lower().endswith(".ics") for fn in email.attachment_filenames if fn
        ) or any(
            m in ("text/calendar", "application/ics")
            for m in email.attachment_mime_types
            if m
        )
        if has_ics != condition.has_calendar_invite:
            return _result(False)

    if condition.has_pdf is not None:
        has_pdf = any(
            m == "application/pdf" for m in email.attachment_mime_types if m
        ) or any(fn.lower().endswith(".pdf") for fn in email.attachment_filenames if fn)
        if has_pdf != condition.has_pdf:
            return _result(False)

    if condition.has_image is not None:
        has_image = any(
            m.startswith("image/") for m in email.attachment_mime_types if m
        )
        if has_image != condition.has_image:
            return _result(False)

    if condition.has_spreadsheet is not None:
        spreadsheet_mimes = {
            "application/vnd.ms-excel",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/vnd.oasis.opendocument.spreadsheet",
            "text/csv",
        }
        spreadsheet_exts = {".xls", ".xlsx", ".csv", ".ods"}
        has_spreadsheet = any(
            m in spreadsheet_mimes for m in email.attachment_mime_types if m
        ) or any(
            any(fn.lower().endswith(ext) for ext in spreadsheet_exts)
            for fn in email.attachment_filenames
            if fn
        )
        if has_spreadsheet != condition.has_spreadsheet:
            return _result(False)

    # Body is mostly links
    if condition.body_is_mostly_links is not None:
        is_mostly = _is_mostly_links(email.body)
        _trace(f"body_is_mostly_links: {is_mostly} == {condition.body_is_mostly_links}")
        if is_mostly != condition.body_is_mostly_links:
            return _result(False)

    # Intent matching (requires LLM client)
    if condition.subject_intent is not None:
        if llm_client is None:
            # Can't evaluate intent without LLM client - fail closed
            _trace("subject_intent: no LLM client, failing")
            return _result(False)

        intent_config = condition.subject_intent
        if isinstance(intent_config, str):
            # Reference to named intent
            if intents is None or intent_config not in intents:
                _trace(f"subject_intent: intent '{intent_config}' not found")
                return _result(False)
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
            return _result(False)

    # Email intent matching (full email context, requires LLM client)
    if condition.email_intent is not None:
        if llm_client is None:
            return _result(False)

        intent_config = condition.email_intent
        if isinstance(intent_config, str):
            # Reference to named intent
            if intents is None or intent_config not in intents:
                return _result(False)
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
            return _result(False)

    # Email category matching (multi-class classifier, requires LLM client)
    if condition.email_category is not None:
        if llm_client is None:
            return _result(False)

        # Parse "category_name/expected_value" format
        if "/" not in condition.email_category:
            log.warning(
                "Invalid email_category format, expected 'category_name/value'",
                email_category=condition.email_category,
            )
            return _result(False)

        category_name, expected_value = condition.email_category.split("/", 1)

        if not category_name or not expected_value:
            log.warning(
                "Invalid email_category format, name or value is empty",
                email_category=condition.email_category,
            )
            return _result(False)

        if email_categories is None or category_name not in email_categories:
            log.warning(
                "Email category not found in config",
                category_name=category_name,
            )
            return _result(False)

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
            return _result(False)

    # Compound conditions
    # any_of: at least one sub-condition must match (OR)
    if condition.any_of is not None:
        any_matched = any(
            matches_condition(sub, email, llm_client, intents, email_categories, trace)
            for sub in condition.any_of
        )
        _trace(f"any_of: {any_matched}")
        if not any_matched:
            return _result(False)

    # all_of: all sub-conditions must match (AND)
    if condition.all_of is not None:
        all_matched = all(
            matches_condition(sub, email, llm_client, intents, email_categories, trace)
            for sub in condition.all_of
        )
        _trace(f"all_of: {all_matched}")
        return _result(all_matched)

    # All conditions passed
    return _result(True)


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

        # Resolve variables if defined
        var_ctx: VariableContext | None = None
        if rule.variables:
            try:
                var_ctx = resolve_variables(email, rule.variables, llm_client)
                _trace.append(
                    f"  rule[{i}]: resolved variables {list(var_ctx.values.keys())}"
                )
            except VariableError as e:
                _trace.append(
                    f"  rule[{i}]: variable resolution failed: {e}, continuing"
                )
                continue  # Fall through to next rule

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
                extract_fields=rule.llm.extract,
            )
            # Override model if specified in rule
            if rule.llm.model:
                model = rule.llm.model

            # Use extraction-aware classification if extract fields specified
            if rule.llm.extract:
                category, confidence, reasoning, extracted = (
                    llm_client.classify_with_extraction(prompt, model, rule.llm.extract)
                )
                # Build variable context from extracted values
                llm_var_ctx = VariableContext(values=extracted) if extracted else None
                _trace.append(
                    f"  rule[{i}]: LLM classified as '{category}' "
                    f"(confidence={confidence:.2f}, extracted={list(extracted.keys())})"
                )
            else:
                category, confidence, reasoning = llm_client.classify(prompt, model)
                llm_var_ctx = None
                _trace.append(
                    f"  rule[{i}]: LLM classified as '{category}' "
                    f"(confidence={confidence:.2f})"
                )

            if category in rule.routes:
                # Merge standalone variables with LLM-extracted variables
                # LLM-extracted variables take precedence in case of collision
                merged_values = (var_ctx.values if var_ctx else {}) | (
                    llm_var_ctx.values if llm_var_ctx else {}
                )
                merged_ctx = (
                    VariableContext(values=merged_values) if merged_values else None
                )

                action = _render_action(
                    rule.routes[category], config.label_prefix, merged_ctx
                )
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
                    variables=merged_ctx.values if merged_ctx else {},
                )
            # Category not in routes - continue
            _trace.append(
                f"  rule[{i}]: category '{category}' not in routes, continuing"
            )
            continue

        # Handle action
        if rule.action:
            action = _render_action(rule.action, config.label_prefix, var_ctx)
            _trace.append(f"  rule[{i}]: matched with action {action}")
            return EvaluationResult(
                matched=True,
                chain=chain_name,
                rule_index=i,
                action=action,
                trace=_trace,
                variables=var_ctx.values if var_ctx else {},
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
