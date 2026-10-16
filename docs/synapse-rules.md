# Synapse Rules Language

Synapse is a declarative rule language for email classification in Cortex Triage. Like synapses routing signals through a brain, Synapse routes emails through chains of rules to determine their fate.

## Overview

Synapse uses a firewall-style architecture inspired by iptables:
- Rules are organized into **chains**
- Within a chain, rules are evaluated top-to-bottom, **first match wins**
- Rules can **jump** to other chains for sub-evaluation
- Unmatched emails fall through to the next rule

Unlike iptables, **chains run in parallel**: every chain marked `auto_run: true`
is evaluated independently against every email and the results are merged (see
[Parallel Chains and the Merge](#parallel-chains-and-the-merge)). `main` is the
only chain that is `auto_run` by default.

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

All matching is **case-insensitive**.

### Universal Match Types

Most fields support four match types:

| Suffix | Description | Example |
|--------|-------------|---------|
| (none) | Exact match | `from: "test@example.com"` |
| `_glob` | Glob pattern (`*`, `?`) | `from_glob: "*@github.com"` |
| `_contains` | Substring match | `from_contains: "github"` |
| `_regex` | Regular expression | `from_regex: ".*@(github|gitlab)\\.com"` |

### Sender/Recipient Fields

| Field | Type | Description |
|-------|------|-------------|
| `from` | string/list | Exact sender address match |
| `from_glob` | string/list | Glob pattern for sender |
| `from_contains` | string/list | Substring match in sender |
| `from_regex` | string/list | Regex pattern for sender |
| `to` | string/list | Exact recipient address match |
| `to_glob` | string/list | Glob pattern for recipient |
| `to_contains` | string/list | Substring match in recipient |
| `to_regex` | string/list | Regex pattern for recipient |
| `cc` | string/list | Exact CC address match |
| `cc_glob` | string/list | Glob pattern for CC |
| `cc_contains` | string/list | Substring match in CC |
| `cc_regex` | string/list | Regex pattern for CC |
| `bcc` | string/list | Exact BCC address match |
| `bcc_glob` | string/list | Glob pattern for BCC |
| `bcc_contains` | string/list | Substring match in BCC |
| `bcc_regex` | string/list | Regex pattern for BCC |
| `deliveredto` | string/list | Exact Delivered-To header match |
| `deliveredto_glob` | string/list | Glob pattern for Delivered-To |
| `deliveredto_contains` | string/list | Substring match in Delivered-To |
| `deliveredto_regex` | string/list | Regex pattern for Delivered-To |

### Subject/Body Fields

| Field | Type | Description |
|-------|------|-------------|
| `subject` | string/list | Exact subject match |
| `subject_glob` | string/list | Glob pattern for subject |
| `subject_contains` | string/list | Substring match in subject |
| `subject_regex` | string/list | Regex pattern for subject |
| `body_contains` | string/list | Substring match in body |
| `body_glob` | string/list | Glob pattern for body |
| `body_regex` | string/list | Regex pattern for body |
| `subject_intent` | string/object | LLM-based intent detection |

### Header Fields

| Field | Type | Description |
|-------|------|-------------|
| `list_id` | string/list | Exact List-ID header match |
| `list_id_glob` | string/list | Glob pattern for List-ID |
| `list_id_contains` | string/list | Substring match in List-ID |
| `list_id_regex` | string/list | Regex pattern for List-ID |
| `reply_to` | string/list | Exact Reply-To match |
| `reply_to_glob` | string/list | Glob pattern for Reply-To |
| `reply_to_contains` | string/list | Substring match in Reply-To |
| `reply_to_regex` | string/list | Regex pattern for Reply-To |
| `header` | dict | Match specific header value (e.g., `{"X-Priority": "1"}`) |
| `header_glob` | dict | Glob pattern for header value |
| `header_contains` | dict | Substring match in header value |
| `header_regex` | dict | Regex pattern for header value |
| `header_exists` | string/list | Check if header exists |
| `header_name_glob` | dict | Glob pattern for both header name AND value |
| `header_name_regex` | dict | Regex pattern for both header name AND value |

```yaml
# Match emails with any X-* header containing "spam"
match:
  header_name_glob:
    "X-*": "*spam*"
```

### Status Conditions

| Field | Type | Description |
|-------|------|-------------|
| `is_read` | boolean | Whether email is read (not UNREAD) |
| `is_starred` | boolean | Whether email is starred |
| `is_important` | boolean | Whether email is marked important |
| `in_inbox` | boolean | Whether email is in inbox |
| `has_label` | string/list | Whether email has specific Gmail label(s) |

```yaml
# Match unread starred emails
match:
  is_read: false
  is_starred: true
```

### Size Conditions

| Field | Type | Description |
|-------|------|-------------|
| `size_larger` | integer | Email larger than N bytes |
| `size_smaller` | integer | Email smaller than N bytes |

```yaml
# Match emails larger than 10MB
match:
  size_larger: 10485760
```

### Date Conditions

| Field | Type | Description |
|-------|------|-------------|
| `date_before` | string/int | Email before date (ISO format or ms timestamp) |
| `date_after` | string/int | Email after date |
| `older_than` | string | Email older than duration (e.g., "7d", "2w", "1m", "1y") |
| `newer_than` | string | Email newer than duration |

```yaml
# Match emails from the last week
match:
  newer_than: "7d"

# Match emails before a specific date
match:
  date_before: "2024-01-01"
```

### Attachment Conditions

| Field | Type | Description |
|-------|------|-------------|
| `has_attachment` | boolean | Whether email has attachments |
| `attachment_filename` | string/list | Exact attachment filename match |
| `attachment_filename_glob` | string/list | Glob pattern for filename |
| `attachment_filename_contains` | string/list | Substring match in filename |
| `attachment_filename_regex` | string/list | Regex pattern for filename |
| `attachment_mime` | string/list | Exact MIME type match |
| `attachment_mime_glob` | string/list | Glob pattern for MIME type |
| `attachment_mime_contains` | string/list | Substring match in MIME type |
| `has_calendar_invite` | boolean | Has .ics or text/calendar attachment |
| `has_pdf` | boolean | Has PDF attachment |
| `has_image` | boolean | Has image attachment (image/*) |
| `has_spreadsheet` | boolean | Has Excel/CSV/ODS attachment |

```yaml
# Match emails with PDF invoices
match:
  has_pdf: true
  subject_contains: "invoice"

# Match emails with specific attachment types
match:
  attachment_mime_glob: "application/vnd.ms-*"
```

### Negation

Any match condition can be negated with `negate: true`:

```yaml
# Match emails NOT from github.com
match:
  from_glob: "*@github.com"
  negate: true
```

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
    - from: "ceo@company.com"
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
  label: "Category/Subcategory"  # Apply Gmail label (auto-prefixed)
  add_label: "Work/Projects"     # Apply raw label (no prefix)
  remove_label: "INBOX"          # Remove raw label (no prefix)
  archive: true                   # Remove from inbox
  mark_read: true                 # Mark as read
  star: true                      # Star the email
```

All action fields are optional.

### Label Types

| Field | Prefix Applied | Use Case |
|-------|----------------|----------|
| `label` | Yes (`Cortex/`) | Cortex-managed categorization |
| `add_label` | No | Apply any Gmail label as-is |
| `remove_label` | No | Remove any Gmail label |

The `label` field is prefixed with `label_prefix` (default: `Cortex`), so `label: "GitHub"` becomes `Cortex/GitHub`.

The `add_label` and `remove_label` fields apply/remove labels exactly as specified, useful for interacting with existing Gmail labels or organizational labels outside the Cortex hierarchy.

```yaml
# Example: Archive to a custom folder and remove from inbox
action:
  add_label: "Archive/2024"
  remove_label: "INBOX"
  archive: true
```

Both `add_label` and `remove_label` accept a single string or a list:

```yaml
action:
  add_label: ["Work", "Priority"]
  remove_label: ["Pending", "Review"]
```

## Variables

Variables let you extract values from emails for use in dynamic labels. Define variables on a rule, then use `{{variable_name}}` in label templates.

```yaml
- match:
    list_id_contains: "github.com"
  variables:
    repo:
      header_regex:
        header: "list-id"
        pattern: "^([^<\\s]+)"  # Captures "owner/repo"
  action:
    label: "GitHub/{{repo}}"
```

### Extraction Types

| Type | Description | Fields |
|------|-------------|--------|
| `header_regex` | Extract from header | `header`, `pattern`, `group` |
| `subject_regex` | Extract from subject | `pattern`, `group` |
| `body_regex` | Extract from body | `pattern`, `group` |
| `from_regex` | Extract from sender | `pattern`, `group` |
| `to_regex` | Extract from recipient | `pattern`, `group` |
| `cc_regex` | Extract from CC | `pattern`, `group` |
| `attachment_filename_regex` | Extract from attachment filename | `pattern`, `group` |
| `llm` | Extract via LLM | `prompt`, `model` |

Each regex type uses a capture group (default: group 1) to extract the value.

```yaml
variables:
  # Extract order ID from subject
  order_id:
    subject_regex:
      pattern: "Order #(\\d+)"
      group: 1

  # Extract username from sender
  sender_name:
    from_regex:
      pattern: "([^@]+)@"

  # Extract from email body
  tracking_number:
    body_regex:
      pattern: "Tracking: ([A-Z0-9]+)"

  # Extract from attachment filename
  invoice_number:
    attachment_filename_regex:
      pattern: "invoice_(\\d+)\\.pdf"

  # LLM extraction for complex cases
  project_name:
    llm:
      prompt: "Extract the project name from this email. Return only the name."
      model: "qwen2.5:0.5b"
```

### Variable Resolution

- All variables must resolve successfully for the rule to match
- If any variable fails to extract, the rule is skipped (falls through to next rule)
- Variables are resolved after match conditions pass, before actions execute

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

## Parallel Chains and the Merge

A chain is either a *root* chain or a *subroutine*:

```yaml
chains:
  main:
    auto_run: true       # evaluated against every email
    rules: [...]

  receipts:
    auto_run: false      # default - only reachable via `jump: receipts`
    rules: [...]

  expense_scan:
    auto_run: true       # ALSO evaluated against every email, independently
    rules: [...]
```

`main` defaults to `auto_run: true`; **every other chain defaults to `false`**.
A chain with `auto_run: false` and no inbound `jump` is dead code.

Each `auto_run` chain produces at most one action (first match wins inside it).
Those actions, plus a virtual result for the sender's priority email mapping, are
merged:

| Field | Merge rule |
|-------|-----------|
| `label`, `add_label`, `remove_label` | **Union.** Purely additive — an email can end up labelled by several chains. |
| `archive`, `mark_read` | **Conservative: `all()`** over the chains that have an opinion. One `false` suppresses it. |
| `star`, `important` | **Aggressive: `any()`.** One `true` wins. |
| `force_archive`, `force_mark_read` | Veto → forces `true`. |
| `force_no_star`, `force_no_important` | Veto → forces `false`. |
| `trash` | **Any chain** setting it asks for Gmail Trash, held until Devon approves the sender (see CLAUDE.md, cortex-gco6). |

Omitting one of the four OPINION flags means "no opinion". Setting it to
`false` is a **vote**, and a single dissenting vote blocks `archive` /
`mark_read` for the whole email.

The four **vetoes** above behave differently: they are gated on truthiness, so
`force_no_star: false` is indistinguishable from omitting it. Only `true` does
anything.

### Writing an additive scan chain

```yaml
  expense_scan:
    auto_run: true
    rules:
      - match:
          subject_contains: ["receipt", "invoice"]
        action:
          add_label: "Expense/2026"
          # NO archive, NO mark_read - deliberately silent.
```

Adding `archive: false` there would look harmless and would instead stop every
other chain from archiving those emails. If a chain genuinely needs to override
the others, use `force_archive` / `force_mark_read`.

### Last resorts

1. Fallback email mappings fire only if **no** chain produced a label. A chain
   that matched without emitting a label does not suppress them.
2. `default_label` fires only if nothing at all — no chain, no fallback mapping —
   produced a label.

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

### Classification Prompts

Prompts are versioned and configurable. The built-in `v1` prompt categorizes emails into:
- `automated_noise` - GitHub notifications, service alerts, marketing
- `human_request` - Personal emails requiring response
- `action_item` - Has deadline (invoices, bills, renewals)
- `wrong_email` - Misdirected mail
- `subscription` - Newsletters, recurring service emails
- `school` - Kids' school communications

You can override or add prompts in your rules file:

```yaml
version: 1
label_prefix: "Cortex"

prompts:
  v1:
    # Override categories for the default prompt
    categories:
      - automated_noise
      - human_request
      - urgent
      - newsletter

  v2:
    # Define a completely custom prompt
    template: |
      Classify this email for a busy professional.
      Categories: {categories}
      From: {from_addr}
      Subject: {subject}
      Body: {body_preview}
      Respond with JSON, for example: {{"category": "priority", "confidence": 0.9}}
    categories:
      - priority
      - delegate
      - archive
    model: "llama3.2:3b"

chains:
  main:
    - llm:
        prompt_version: "v2"  # Use custom prompt
      routes:
        priority:
          label: "Priority"
          star: true
        delegate:
          label: "Delegate"
        archive:
          archive: true
```

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
          - from: "boss@company.com"
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
3. **No catch-all in `main`**: a `match: {}` rule that applies a default label
   counts as "produced a label" and silently suppresses every fallback email
   mapping. Let `default_label` handle the unmatched case. Catch-alls inside a
   *subroutine* chain reached by `jump` are fine.
4. **Lists for OR**: Use lists instead of multiple rules for OR conditions
5. **LLM as fallback**: Use LLM classification only for ambiguous cases
6. **Test your rules**: Validate with `validate_rules()` before deploying

## Configuration

**There is no rules file.** `RULES_PATH` is dead — the triage worker loads the
active config from Postgres (`triage_config_versions` / `triage_chains` /
`triage_rules`).

This YAML is the wire format of the Gateway `/config` API:

The gateway's external port has required a bearer token since 2026-09-15
(`CORTEX_API_TOKEN`; only `/health`, `/metrics` and `/oauth/callback` are
exempt -- `/metrics` because Prometheus scrapes it), so
these 401 without the header. Peer services calling the internal port do not
need it.

```bash
AUTH="Authorization: Bearer $CORTEX_API_TOKEN"

curl -s -H "$AUTH" http://<gateway>/config > /tmp/rules.yaml   # export active
curl -s -X POST http://<gateway>/config/validate -H "$AUTH" \
  -H 'Content-Type: text/plain' --data-binary @/tmp/rules.yaml
curl -s -X PUT http://<gateway>/config -H "$AUTH" \
  -H 'Content-Type: text/plain' -H 'X-Created-By: you@context' \
  -H 'X-Notes: what changed' --data-binary @/tmp/rules.yaml
```

The PUT activates the new version and signals the worker to reload; no restart.

Two things to know before you use it:

- **Email mappings are NOT managed here.** `priority_email_mappings` /
  `fallback_email_mappings` appear in the export but the importer is
  upsert-only — removing one from the YAML is a silent no-op. Use the Gateway
  `/mappings` CRUD API instead.
- **A new version re-labels historical mail.** The worker converges old
  classifications to the active config version during idle time, newest first.
  That is by design; see `docs/project-spec.md` → "Convergence".

Full workflow and current hazards: `~/Projects/cortex/CLAUDE.md` → "Triage
Config Management".
