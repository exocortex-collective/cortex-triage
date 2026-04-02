"""CRDT action merging for parallel chain evaluation.

When multiple auto_run chains evaluate an email independently, their results
are merged using CRDT (Conflict-free Replicated Data Type) semantics:

Labels: Union of all labels from all chains. Purely additive.

Conservative flags (false wins — keep visible unless unanimous):
  - archive: Only archives if ALL chains agree.
  - mark_read: Only marks read if ALL chains agree.

Aggressive flags (true wins — surface if anyone thinks so):
  - star: Stars if ANY chain says so.
  - important: Marks important if ANY chain says so.

Vetoes (override merge result in opposite direction):
  - not_archive: Force archive regardless of other chains.
  - not_unread: Force mark-read regardless of other chains.
  - not_star: Force no star regardless of other chains.
  - not_important: Force not important regardless of other chains.
"""

from cortex_utils.triage_config.models import Action, EvaluationResult


def merge_actions(results: list[EvaluationResult]) -> Action | None:
    """Merge actions from multiple chain evaluations using CRDT semantics.

    Args:
        results: List of evaluation results from parallel chain runs.
            Unmatched results (matched=False) are excluded from merge.

    Returns:
        Merged Action combining all labels and flags, or None if no
        chains matched.
    """
    matched = [r for r in results if r.matched and r.action is not None]
    if not matched:
        return None

    # Collect all labels
    all_labels: list[str] = []
    all_remove_labels: list[str] = []

    # Track flag opinions (None = no opinion)
    archive_opinions: list[bool] = []
    mark_read_opinions: list[bool] = []
    star_opinions: list[bool] = []
    important_opinions: list[bool] = []

    # Track vetoes
    has_not_archive = False
    has_not_unread = False
    has_not_star = False
    has_not_important = False

    for r in matched:
        action = r.action
        assert action is not None

        # Collect labels
        if action.label:
            all_labels.append(action.label)
        if action.add_label:
            if isinstance(action.add_label, list):
                all_labels.extend(action.add_label)
            else:
                all_labels.append(action.add_label)
        if action.remove_label:
            if isinstance(action.remove_label, list):
                all_remove_labels.extend(action.remove_label)
            else:
                all_remove_labels.append(action.remove_label)

        # Collect flag opinions (only if chain has an opinion)
        if action.archive is not None:
            archive_opinions.append(action.archive)
        if action.mark_read is not None:
            mark_read_opinions.append(action.mark_read)
        if action.star is not None:
            star_opinions.append(action.star)
        if action.important is not None:
            important_opinions.append(action.important)

        # Collect vetoes
        if action.not_archive:
            has_not_archive = True
        if action.not_unread:
            has_not_unread = True
        if action.not_star:
            has_not_star = True
        if action.not_important:
            has_not_important = True

    # Merge conservative flags (false wins)
    archive = all(archive_opinions) if archive_opinions else None
    mark_read = all(mark_read_opinions) if mark_read_opinions else None

    # Merge aggressive flags (true wins)
    star = any(star_opinions) if star_opinions else None
    important = any(important_opinions) if important_opinions else None

    # Apply vetoes
    if has_not_archive:
        archive = True
    if has_not_unread:
        mark_read = True
    if has_not_star:
        star = False
    if has_not_important:
        important = False

    # Deduplicate labels while preserving order
    seen: set[str] = set()
    unique_labels: list[str] = []
    for label in all_labels:
        if label not in seen:
            seen.add(label)
            unique_labels.append(label)

    seen_remove: set[str] = set()
    unique_remove: list[str] = []
    for label in all_remove_labels:
        if label not in seen_remove:
            seen_remove.add(label)
            unique_remove.append(label)

    return Action(
        add_label=unique_labels if unique_labels else None,
        remove_label=unique_remove if unique_remove else None,
        archive=archive,
        mark_read=mark_read,
        star=star,
        important=important,
    )
