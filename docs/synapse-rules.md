# Synapse Rules Language

Synapse is a declarative rule language for email classification in Cortex Triage. Like synapses routing signals through a brain, Synapse routes emails through chains of rules to determine their fate.

## Overview

Synapse uses a firewall-style architecture inspired by iptables:
- Rules are organized into **chains**
- Evaluation starts at the `main` chain
- Rules are evaluated top-to-bottom, **first match wins**
- Rules can **jump** to other chains for sub-evaluation
- Unmatched emails fall through to the next rule

## Quick Start

```yaml
version: 1
label_prefix: "Cortex"

chains:
  main:
    # Match GitHub notifications, archive them
    - match:
        from_glob: "*@github.com"
      action:
        label: "Automated/GitHub"
        archive: true

    # Everything else gets classified by LLM
    - match: {}
      jump: llm_classify
```

## Rule Structure

Each rule has two parts: **match** (when to trigger) and **outcome** (what to do).

```yaml
- match:
    from_glob: "*@example.com"
    subject_contains: "invoice"
  action:
    label: "Finance/Invoices"
    star: true
```

### Outcomes

A rule must have exactly one outcome:

| Outcome | Description |
|---------|-------------|
| `action` | Apply labels, archive, star, or mark read |
| `jump` | Delegate to another chain |
| `llm` | Use LLM classification with routes |

## Match Conditions

### Basic Fields

| Field | Type | Description |
|-------|------|-------------|
| `from` | string/list | Exact sender address match |
| `to` | string/list | Exact recipient address match |
| `from_glob` | string/list | Glob pattern for sender (e.g., `*@github.com`) |
| `to_glob` | string/list | Glob pattern for recipient |
| `from_contains` | string/list | Substring match in sender |
| `to_contains` | string/list | Substring match in recipient |
| `subject_contains` | string/list | Substring match in subject |
| `body_contains` | string/list | Substring match in body |
| `subject_regex` | string/list | Regex pattern for subject |
| `has_attachment` | boolean | Whether email has attachments |
| `subject_intent` | string/object | LLM-based intent detection |

All matching is **case-insensitive**.

### Logic Rules

**Within a field:** Lists are **OR** (any value matches)
```yaml
# Matches if from github.com OR gitlab.com
from_glob: ["*@github.com", "*@gitlab.com"]
```

**Across fields:** Multiple fields are **AND** (all must match)
```yaml
# Must be from GitHub AND contain "bug" in subject
match:
  from_glob: "*@github.com"
  subject_contains: "bug"
```

**Empty match:** Matches everything (catch-all)
```yaml
match: {}
```

### Compound Conditions

For complex logic, use `any_of` and `all_of`:

| Operator | Logic | Description |
|----------|-------|-------------|
| `any_of` | OR | At least one sub-condition must match |
| `all_of` | AND | All sub-conditions must match |

```yaml
# Match if from VIP OR subject is urgent
match:
  any_of:
    - from_addr: "ceo@company.com"
    - from_glob: "*@board.company.com"
    - subject_contains: "URGENT"
```

```yaml
# Match GitHub bugs that are also critical
match:
  from_glob: "*@github.com"
  all_of:
    - subject_contains: "bug"
    - subject_contains: "critical"
```

Compound conditions can be nested and combined:

```yaml
# (from GitHub OR GitLab) AND (bug AND critical)
match:
  any_of:
    - from_glob: "*@github.com"
    - from_glob: "*@gitlab.com"
  all_of:
    - subject_contains: "bug"
    - subject_contains: "critical"
```

## Actions

Actions define what happens when a rule matches:

```yaml
action:
  label: "Category/Subcategory"  # Apply Gmail label
  archive: true                   # Remove from inbox
  mark_read: true                 # Mark as read
  star: true                      # Star the email
```

All action fields are optional. Labels are prefixed with `label_prefix` (default: `Cortex`), so `label: "GitHub"` becomes `Cortex/GitHub`.

## Chains and Jumps

Chains let you organize rules into logical groups:

```yaml
chains:
  main:
    - match:
        from: "me@example.com"
        to: "me@example.com"
      jump: self_note  # Delegate to self_note chain

    - match: {}
      action:
        label: "Inbox"

  self_note:
    - match:
        subject_contains: "todo"
      action:
        label: "Todo"

    - match: {}
      action:
        label: "Self-Note"
```

**Jump behavior:**
- If the target chain matches, that result is returned
- If the target chain doesn't match, evaluation **continues** to the next rule
- Circular jumps are detected and prevented

## LLM Classification

For emails that don't match simple rules, use LLM classification:

```yaml
- llm:
    model: "qwen2.5:7b"
    prompt_version: "v1"
  routes:
    human_request:
      label: "Human/NeedsResponse"
      star: true
    automated_noise:
      label: "Automated"
      archive: true
      mark_read: true
    newsletter:
      label: "Commercial/Newsletter"
```

The LLM returns a category, which is mapped to an action via `routes`.

## Intent Matching

For lightweight LLM-based matching within conditions, use `subject_intent`:

```yaml
# Reference a built-in or configured intent
- match:
    subject_intent: "archive_request"
  action:
    label: "Archiver"

# Or define inline
- match:
    subject_intent:
      prompt: "Does this subject indicate a todo item? Subject: {subject}"
      model: "qwen2.5:0.5b"
  action:
    label: "Todo"
```

### Built-in Intents

| Intent | Description |
|--------|-------------|
| `archive_request` | Wants to save/bookmark something |
| `todo_request` | Task, reminder, or todo item |
| `question` | Question requiring a response |

## Complete Example

```yaml
version: 1
label_prefix: "Cortex"

chains:
  main:
    # Self-to-self emails
    - match:
        from: "me@example.com"
        to: "me@example.com"
      jump: self_note

    # DevOps alerts - bugs get starred
    - match:
        from_glob: ["*@github.com", "*@gitlab.com", "*@bitbucket.org"]
        subject_contains: ["bug", "error", "failed", "critical"]
      action:
        label: "DevOps/Alerts"
        star: true

    # Other DevOps noise
    - match:
        from_glob: ["*@github.com", "*@gitlab.com", "*@bitbucket.org"]
      action:
        label: "Automated/DevOps"
        archive: true
        mark_read: true

    # Newsletters
    - match:
        from_glob:
          - "*@mailchimp.com"
          - "*@sendgrid.net"
          - "*@substack.com"
      action:
        label: "Commercial/Newsletter"
        archive: true

    # VIP senders always get attention
    - match:
        any_of:
          - from_addr: "boss@company.com"
          - from_glob: "*@important-client.com"
          - subject_regex: "\\[URGENT\\]"
      action:
        label: "Priority"
        star: true

    # Fallback to LLM
    - match: {}
      jump: llm_classify

  self_note:
    - match:
        subject_intent: "archive_request"
      action:
        label: "Archiver"

    - match:
        subject_intent: "todo_request"
      action:
        label: "Todo"

    - match: {}
      action:
        label: "Self-Note"

  llm_classify:
    - llm:
        model: "qwen2.5:7b"
        prompt_version: "v1"
      routes:
        human_request:
          label: "Human/NeedsResponse"
          star: true
        action_item:
          label: "Action/Required"
          star: true
        newsletter:
          label: "Commercial/Newsletter"
          archive: true
        automated:
          label: "Automated"
          archive: true
          mark_read: true
```

## Best Practices

1. **Order matters**: Put specific rules before general ones
2. **Use chains**: Group related rules for clarity
3. **Catch-all last**: End chains with `match: {}` to handle unmatched emails
4. **Lists for OR**: Use lists instead of multiple rules for OR conditions
5. **LLM as fallback**: Use LLM classification only for ambiguous cases
6. **Test your rules**: Validate with `validate_rules()` before deploying

## Configuration

Set the rules file path via environment variable:

```bash
export RULES_PATH=/path/to/rules.yaml
```

Or use the default: `config/rules.yaml`
