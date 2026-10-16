"""Which part of the config decided a classification (triage-70be).

A footprint records a hash of the global settings (label_prefix, default_label,
which chains auto_run) and, for every chain an email entered, how far
evaluation got plus a hash of everything up to that point: the rules
themselves and the intent, category and prompt definitions they call. A config
change can only alter an email's result if it changes one of those hashes, so
convergence can re-triage just the emails whose footprint no longer matches.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from cortex_utils.triage_config.models import Rule, RulesConfig

FOOTPRINT_VERSION = 1


def _digest(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, default=str).encode()
    return hashlib.sha256(raw).hexdigest()[:16]


def _llm_refs(condition: Any, config: RulesConfig, refs: dict[str, Any]) -> None:
    if isinstance(condition, list):
        for c in condition:
            _llm_refs(c, config, refs)
        return
    if not isinstance(condition, dict):
        return
    for key in ("email_intent", "subject_intent"):
        name = condition.get(key)
        if isinstance(name, dict):  # intent defined inline; already in the rule
            continue
        if name:
            spec = config.intents.get(name)
            refs[f"intent:{name}"] = spec.model_dump(mode="json") if spec else None
    category = condition.get("email_category")
    if category:
        name = category.split("/", 1)[0]
        category_spec = config.email_categories.get(name)
        refs[f"category:{name}"] = (
            category_spec.model_dump(mode="json") if category_spec else None
        )
    for key in ("any_of", "all_of"):
        _llm_refs(condition.get(key), config, refs)


def _rule_identity(rule: Rule, config: RulesConfig) -> str:
    body = rule.model_dump(mode="json")
    refs: dict[str, Any] = {}
    _llm_refs(body.get("match"), config, refs)
    if body.get("llm") or body.get("variables"):
        refs["prompts"] = {
            k: v.model_dump(mode="json") for k, v in config.prompts.items()
        }
        refs["body_extraction_prompts"] = {
            k: v.model_dump(mode="json")
            for k, v in config.body_extraction_prompts.items()
        }
    return _digest([body, refs])


def _global_identity(config: RulesConfig) -> str:
    auto_run = sorted(name for name, chain in config.chains.items() if chain.auto_run)
    return _digest([config.label_prefix, config.default_label, auto_run])


def chain_hashes(config: RulesConfig) -> dict[str, list[str]]:
    """Cumulative hash of each chain's rules: entry i covers rules 0..i."""
    hashes: dict[str, list[str]] = {}
    for name, chain in config.chains.items():
        running = _digest(name)
        cumulative = []
        for rule in chain.rules:
            running = _digest([running, _rule_identity(rule, config)])
            cumulative.append(running)
        hashes[name] = cumulative
    return hashes


def _entry_hash(
    hashes: dict[str, list[str]], chain: str, stop: int | None
) -> str | None:
    cumulative = hashes.get(chain)
    if stop == -1:  # not evaluated: a cycle, or a jump to a missing chain
        return _digest([chain, "unevaluated", cumulative is not None])
    if cumulative is None:
        return None
    if stop is None:
        return cumulative[-1] if cumulative else _digest([chain, "empty"])
    return cumulative[stop] if stop < len(cumulative) else None


def build_footprint(
    config: RulesConfig,
    hashes: dict[str, list[str]],
    path: list[tuple[str, int | None]],
) -> dict[str, Any]:
    entries = []
    for chain, stop in path:
        entry = [chain, stop, _entry_hash(hashes, chain, stop)]
        if entry not in entries:
            entries.append(entry)
    return {"v": FOOTPRINT_VERSION, "g": _global_identity(config), "c": entries}


def is_affected(
    footprint: dict[str, Any] | None, config: RulesConfig, hashes: dict[str, list[str]]
) -> bool:
    """Could re-triaging under `config` give a different result? True when unsure."""
    try:
        if not footprint or footprint.get("v") != FOOTPRINT_VERSION:
            return True
        if footprint.get("g") != _global_identity(config):
            return True
        for chain, stop, digest in footprint["c"]:
            if digest is None or _entry_hash(hashes, chain, stop) != digest:
                return True
    except (AttributeError, KeyError, TypeError, ValueError, IndexError):
        return True  # a malformed footprint proves nothing
    return False
