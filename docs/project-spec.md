# Triage - Email Classification & Routing

**Project**: cortex-triage
**Purpose**: Rule-based email classification and workflow dispatch, with LLM fallback

## Overview

Triage consumes emails from postmark's queue and routes them to workflows using a firewall-style rules engine. Deterministic rules are evaluated first (fast, cheap), with LLM classification as a fallback chain for fuzzy matching.

## Architecture

```
         ┌───────────────┐
         │   postmark    │
         │   (queue)     │
         └───────┬───────┘
                 │ triage queue
                 ▼
         ┌───────────────┐
         │ triage worker │  (Python service)
         │  rules engine │
         └───────┬───────┘
                 │
        ┌────────┼────────┬─────────────┐
        ▼        ▼        ▼             ▼
   ┌─────────┐ ┌──────┐ ┌──────┐  ┌──────────┐
   │archiver │ │ n8n  │ │Gmail │  │  Ollama  │
   │ (queue) │ │(hook)│ │labels│  │(fallback)│
   └─────────┘ └──────┘ └──────┘  └──────────┘
```

## Rules Engine

### Mental Model: Firewall Rules

Like iptables/nftables:
- **Rules evaluated in order** - first match wins (or jumps to chain)
- **Chains** - named rulesets for sub-evaluation
- **Match conditions** - from, to, subject patterns, has_attachment, etc.
- **Actions** - dispatch to workflow, apply label, enqueue, call webhook

### Rule Evaluation

```
1. Load rules from YAML
2. Start at "main" chain
3. For each rule in order:
   a. Evaluate match conditions against email
   b. If match:
      - If "jump": evaluate that chain, then continue or stop based on result
      - If "action": execute action and STOP (first match wins)
   c. If no match: continue to next rule
4. If no rules match: fall through (no action taken)
```

### Rules Configuration (YAML)

```yaml
# rules.yaml
version: 1

chains:
  main:
    # Self-to-self emails get special handling
    - match:
        from: "devon.jones@gmail.com"
        to: "devon.jones@gmail.com"
      jump: self_note

    # GitHub noise - archive silently
    - match:
        from_glob: "*@github.com"
      action:
        label: "Automated/GitHub"
        archive: true
        mark_read: true

    # Known noise domains
    - match:
        from_glob: "*@linkedin.com"
      action:
        label: "Automated/Social"
        archive: true

    # School communications
    - match:
        from_glob: "*@*.k12.*"
      action:
        label: "School"

    # Property management
    - match:
        from_contains: "realpropertymgt"
      action:
        label: "Landlord"
        star: true

    # Fallback: LLM classification
    - match: {}
      jump: llm_classify

  self_note:
    # Archive workflow for URLs
    - match:
        subject_contains: ["archive", "save", "clip", "read later"]
      action:
        workflow: archiver

    # Todo items via n8n
    - match:
        subject_contains: ["todo", "remind", "task"]
      action:
        webhook: "n8n/todo-from-email"

    # Unmatched self-notes
    - match: {}
      action:
        label: "Self-Note"

  llm_classify:
    # This chain calls the LLM, then routes based on category
    # The triage worker handles this specially
    - llm:
        model: "qwen2.5:7b"
        prompt_version: "v1"
      routes:
        human_request:
          label: "Needs Response"
          star: true
        action_item:
          label: "Action Required"
          star: true
        wrong_email:
          label: "Wrong Email"
          archive: true
        subscription:
          label: "Subscription"
        school:
          label: "School"
        automated_noise:
          label: "Automated"
          archive: true
          mark_read: true
```

### Match Conditions

| Condition | Description | Example |
|-----------|-------------|---------|
| `from` | Exact match on from address | `"devon.jones@gmail.com"` |
| `from_glob` | Glob pattern on from address | `"*@github.com"` |
| `from_contains` | Substring match on from | `"realpropertymgt"` |
| `to` | Exact match on to address | `"devon.jones@gmail.com"` |
| `to_glob` | Glob pattern on to address | `"*@mycompany.com"` |
| `subject_contains` | Any substring in subject | `["archive", "save"]` |
| `subject_regex` | Regex match on subject | `"^\\[.*\\] PR #\\d+"` |
| `subject_intent` | LLM intent classification (see below) | `"archive_request"` |
| `has_attachment` | Email has attachments | `true` |
| `body_contains` | Substring in body (fetched from DuckDB API) | `["unsubscribe"]` |
| `header` | Glob match on any RFC822 header | `{List-ID: "*googlegroups*"}` |
| `header_contains` | Substring match on any header | `{Reply-To: "noreply"}` |
| `list_id` | Exact match on List-ID header | `"<list.example.com>"` |
| `list_id_glob` | Glob pattern on List-ID | `"*googlegroups*"` |
| `list_id_contains` | Substring in List-ID | `"nycresistor"` |
| `reply_to` | Exact match on Reply-To header | `"noreply@example.com"` |
| `reply_to_glob` | Glob pattern on Reply-To | `"*noreply*"` |
| `reply_to_contains` | Substring in Reply-To | `"do-not-reply"` |
| `{}` | Empty = match all (catch-all) | |

### Intent Matching (subject_intent)

For fuzzy subject matching where you won't remember exact keywords, use a small LLM for intent classification.

**Use case:** Self-note chain where you might write:
- "archive this"
- "save for later"
- "clip: interesting article"
- "bookmark"
- "stash this link"
- "keep for reference"

**Configuration:**
```yaml
self_note:
  # Simple: use built-in intent with defaults
  - match:
      subject_intent: "archive_request"
    action:
      workflow: archiver

  # Or explicit: override model
  - match:
      subject_intent:
        intent: "archive_request"
        model: "qwen2.5:1.5b"  # override default model
    action:
      workflow: archiver

  # Or custom prompt: for one-off intents
  - match:
      subject_intent:
        prompt: |
          Does this subject indicate a recipe or cooking-related link?
          Subject: "{subject}"
          Answer only: yes or no
        model: "qwen2.5:0.5b"
    action:
      label: "Self-Note/Recipes"
```

**Built-in intents (with default prompts):**
```yaml
# These are built into the triage worker
# Users can reference by name, or override the prompt in rules.yaml

intents:
  archive_request:
    prompt: |
      Does this email subject indicate the sender wants to save,
      archive, or bookmark something for later?
      Subject: "{subject}"
      Answer only: yes or no
    model: "qwen2.5:0.5b"

  todo_request:
    prompt: |
      Does this email subject indicate a task, reminder, or todo item?
      Subject: "{subject}"
      Answer only: yes or no
    model: "qwen2.5:0.5b"

  question:
    prompt: |
      Does this email subject contain a question requiring a response?
      Subject: "{subject}"
      Answer only: yes or no
    model: "qwen2.5:0.5b"
```

**Override default prompts in rules.yaml:**
```yaml
# rules.yaml
version: 1

# Override built-in intent defaults
intents:
  archive_request:
    prompt: |
      Is this a request to save, archive, clip, bookmark, or stash something?
      Consider variations like "keep this", "hold onto", "store for later".
      Subject: "{subject}"
      Answer only: yes or no

chains:
  # ... rules reference intents by name ...
```

**Implementation:**
```python
def match_intent(subject: str, intent_config: dict) -> bool:
    prompt = intent_config['prompt'].format(subject=subject)
    response = ollama.generate(
        model=intent_config.get('model', 'qwen2.5:0.5b'),
        prompt=prompt,
    )
    answer = response.strip().lower()
    return answer == 'yes'
```

**Performance:**
- Model: qwen2.5:0.5b (~10ms) or qwen2.5:1.5b (~25ms)
- Only runs when rule is evaluated (after deterministic matches fail)
- Low volume for self-note chain
- Local inference, no API cost

### Header Matching

Match on any RFC822 email header using `header` (glob pattern) or `header_contains` (substring). Headers are fetched from `emails_raw.headers` at triage time, so no schema changes are needed to add new header filters.

**Shorthand aliases** for common headers (List-ID and Reply-To):
```yaml
chains:
  main:
    # Using aliases (recommended for common headers)
    - match:
        list_id_glob: "*googlegroups*"
      action:
        label: "Lists/Google Groups"
        archive: true

    # Substring match on List-ID
    - match:
        list_id_contains: "nycresistor"
      action:
        label: "Lists/NYC Resistor"

    # Reply-To matching
    - match:
        reply_to_glob: "*noreply*"
      action:
        label: "Automated"
```

**Generic header matching** (for any header):
```yaml
    # Match any header using dict format
    - match:
        header:
          X-Mailer: "*Outlook*"
      action:
        label: "From Outlook"

    # Substring match on any header
    - match:
        header_contains:
          Precedence: "bulk"
      action:
        label: "Bulk Mail"
```

**Multiple headers (AND condition):**
```yaml
    # All specified headers must match
    - match:
        list_id_glob: "*googlegroups*"
        reply_to_glob: "*noreply*"
      action:
        label: "Automated Lists"
```

**Common headers:**
- `List-ID` - Mailing list identifier (e.g., `<list.example.com>`)
- `Reply-To` - Address for replies (often indicates automated/no-reply)
- `X-Mailer` - Email client that sent the message
- `X-Spam-Status` - Spam filter status
- `Precedence` - `bulk`, `list`, `junk` for automated emails
- `Auto-Submitted` - `auto-generated`, `auto-replied` for automated emails

### Actions

Triage handles **inbox management only** - applying labels and organizing email. Workflow dispatch (archiver, webhooks, etc.) is handled by the separate `actions` service which watches for label events.

| Action | Description |
|--------|-------------|
| `label` | Apply Gmail label (triggers actions service via label event) |
| `archive` | Archive email (remove from inbox) |
| `mark_read` | Mark as read |
| `star` | Star the email |
| `jump` | Evaluate another chain |

**Why no workflow/webhook actions?** Labels are the API. When triage applies a label like `Cortex/Archiver`, postmark sees the label event and enqueues to the `actions` queue. The actions service then dispatches to workflows. This means:
- Gmail filters can trigger workflows (not just triage rules)
- Manual drag-and-drop to a label triggers workflows
- Replay is easy: re-enqueue all emails with label X

## Services

Triage runs as 2 Docker containers:

| Service | Container | Purpose |
|---------|-----------|---------|
| triage-worker | cortex-triage-worker | Rules engine, LLM classification |
| labeling-worker | cortex-labeling-worker | Gmail label application |

### triage-worker

Dequeues from `triage` queue and:
1. Loads rules (from YAML)
2. Evaluates rules against email
3. If an action is required, enqueues a job for the `labeling-worker`
4. Records classification in `classifications` table

### labeling-worker

Dequeues jobs from the `labeling` queue and:
1. Modifies Gmail labels via the API to apply actions (custom labels, archive, mark_read, star)
2. Marks the job as complete in the queue upon success

**Pseudocode**:
```python
rules = load_rules('rules.yaml')

while True:
    item = db.dequeue('triage')
    if not item:
        time.sleep(5)
        continue

    email = db.fetch_email(item.payload['email_id'])

    # Evaluate rules
    result = evaluate_chain(rules, 'main', email)

    if result.action:
        # Note: actual payload is more detailed (see worker.py _enqueue_labeling)
        db.enqueue('labeling', {
            'gmail_id': item.payload['gmail_id'],
            'action': result.action
        })

    # Record for audit
    db.insert_classification(
        email_id=email.id,
        matched_rule=result.rule_id,
        chain=result.chain,
        action=result.action,
        llm_category=result.llm_category,  # if LLM was used
    )

    db.complete_queue_item(item.id)
```

## Data Model

### Classifications Table

Stores classification history - every evaluation is a new row, enabling re-evaluation and audit.

```sql
CREATE TABLE classifications (
    id BIGSERIAL PRIMARY KEY,
    email_id BIGINT NOT NULL,     -- Logically references emails_raw(id). No FK to preserve classifications for soft-deleted emails.
    gmail_id TEXT NOT NULL,

    -- Rule matching
    matched_chain TEXT,           -- which chain matched (NULL = no match)
    matched_rule_index INT,       -- rule position in chain
    rules_version TEXT,           -- rules.yaml version that was used

    -- LLM (if used)
    llm_category TEXT,            -- category from LLM
    llm_confidence REAL,
    llm_reasoning TEXT,

    -- Action taken
    action_taken JSONB,           -- full action ({label, add_label, remove_label, archive, star, mark_read})
    action_executed BOOLEAN DEFAULT FALSE,  -- set true when action enqueued (does not guarantee final execution)

    classified_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_classifications_email ON classifications(email_id);
CREATE INDEX idx_classifications_gmail ON classifications(gmail_id);
CREATE INDEX idx_classifications_chain ON classifications(matched_chain);
CREATE INDEX idx_classifications_date ON classifications(classified_at DESC);
CREATE INDEX idx_classifications_rules_version ON classifications(rules_version);
```

### Re-evaluation Support

Every classification is immutable - re-evaluation creates a new row. This enables:
- **Audit trail** - see how classification changed over time
- **A/B testing** - compare different rules versions
- **Reprocessing** - re-run with new rules without losing history

**Query: Emails needing classification (never classified)**
```sql
SELECT e.id, e.gmail_id
FROM emails_raw e
LEFT JOIN classifications c ON e.id = c.email_id
WHERE c.id IS NULL;
```

**Query: Emails to re-evaluate (classified with old rules)**
```sql
SELECT DISTINCT e.id, e.gmail_id
FROM emails_raw e
JOIN classifications c ON e.id = c.email_id
WHERE c.rules_version != 'v2'  -- current version
  AND NOT EXISTS (
    SELECT 1 FROM classifications c2
    WHERE c2.email_id = e.id AND c2.rules_version = 'v2'
  );
```

**Query: Latest classification per email**
```sql
SELECT DISTINCT ON (email_id) *
FROM classifications
ORDER BY email_id, classified_at DESC;
```

### Classification Hierarchy

Classifications use path-style hierarchy, nested under `Cortex/` in Gmail:

```
Cortex/
├── Cortex/Automated
│   ├── Cortex/Automated/GitHub
│   ├── Cortex/Automated/CI
│   └── Cortex/Automated/Social
├── Cortex/Commercial
│   ├── Cortex/Commercial/Promotional
│   ├── Cortex/Commercial/Newsletter
│   └── Cortex/Commercial/Transactional
├── Cortex/School
│   ├── Cortex/School/Announcements
│   ├── Cortex/School/PermissionSlips
│   └── Cortex/School/GradeReports
└── Cortex/Human
    ├── Cortex/Human/Family
    ├── Cortex/Human/Work
    └── Cortex/Human/Friends
```

**Benefits:**
- **Clear separation** - Cortex labels vs your manual labels
- **Collapsible** - collapse entire `Cortex/` tree when not relevant
- **Validation workflow** - move email between Cortex labels = training signal
- **Refactor-friendly** - query by prefix: `WHERE label LIKE 'Cortex/Commercial/%'`
- Gmail nested labels map naturally

**Validation via Gmail:**
When you manually move an email from `Cortex/Commercial/Newsletter` to `Cortex/Commercial/Transactional`, that's a correction. Gmail history events (`labelAdded`/`labelRemoved`) let us detect these changes.

### Label Change Detection

Gmail-sync receives history events for label changes. We need to distinguish:
1. **Cortex applied the label** → ignore (our own action)
2. **User changed the label** → record as correction

**Detection logic:**
```python
def handle_label_change(gmail_id: str, new_label: str) -> None:
    # Get latest classification for this email
    latest = db.get_latest_classification(gmail_id)

    if latest is None:
        # Never classified - enqueue for triage
        db.enqueue('triage', {'gmail_id': gmail_id})
        return

    current_label = latest.action_taken.get('label')

    if current_label == new_label:
        # This is our own label application echoing back - ignore
        return

    if not new_label.startswith('Cortex/'):
        # User applied non-Cortex label - not a correction, ignore
        return

    # User correction! Record it
    db.insert_label_correction(
        gmail_id=gmail_id,
        original_label=current_label,
        corrected_label=new_label,
        original_classification_id=latest.id,
    )
```

### Label Corrections Table (Future)

**Note**: This table is not yet implemented. When implemented, it will track user corrections for training/feedback.

```sql
-- Future: track user corrections to classifications
CREATE TABLE label_corrections (
    id BIGSERIAL PRIMARY KEY,
    gmail_id TEXT NOT NULL,
    original_classification_id BIGINT REFERENCES classifications(id),
    original_label TEXT,
    corrected_label TEXT NOT NULL,
    corrected_at TIMESTAMPTZ DEFAULT NOW()
);
```

**Planned uses for corrections:**
- Review misclassifications to improve rules
- Training data for LLM fine-tuning
- Metrics: correction rate by category
- Auto-generate rule suggestions from patterns

### Review Workflow

Spot-check classifications via Gmail, then discuss with Claude to refine rules.

**Workflow:**
1. Browse a label in Gmail (e.g., `Cortex/Commercial/Newsletter`)
2. Spot emails that look wrong
3. Ask Claude: "Read the emails in `Cortex/Commercial/Newsletter` from the last week"
4. Review the list together - discuss which are misclassified and why
5. Identify patterns (e.g., "all receipts from Stripe are getting labeled Newsletter")
6. Write a new rule or adjust LLM prompt
7. Re-run: `triage-worker --reprocess-label "Cortex/Commercial/Newsletter"`

**Example conversation:**
```
User: "The Commercial/Newsletter label has some wrong stuff, take a look"

Claude: *queries classifications table, fetches email metadata*

  | From                  | Subject                        | Classified |
  |-----------------------|--------------------------------|------------|
  | news@example.com      | Weekly digest                  | ✓ correct  |
  | stripe@stripe.com     | Payment receipt for $49        | ✗ wrong    |
  | marketing@saas.io     | New feature announcement       | ✓ correct  |
  | noreply@amazon.com    | Your order has shipped         | ✗ wrong    |

Claude: "Looks like transactional emails (receipts, shipping) are getting
mixed in. These should be Commercial/Transactional. Want me to add a rule?"

User: "Yeah"

Claude: *adds rule*
  - match: { subject_contains: ["receipt", "payment", "order", "shipped"] }
    action: { label: "Commercial/Transactional" }
```

**Query for review:**
```sql
SELECT
    e.gmail_id,
    e.from_addr,
    e.subject,
    c.classified_at,
    c.matched_chain,
    c.llm_category
FROM emails_raw e
JOIN classifications c ON e.id = c.email_id
WHERE c.action_taken->>'label' = 'Cortex/Commercial/Newsletter'
  AND c.classified_at > NOW() - INTERVAL '7 days'
ORDER BY c.classified_at DESC;
```

**In rules.yaml:**
```yaml
action:
  label: "Cortex/Commercial/Newsletter"  # always prefixed
```

**In LLM routes:**
```yaml
routes:
  commercial_newsletter:
    label: "Cortex/Commercial/Newsletter"
  commercial_promotional:
    label: "Cortex/Commercial/Promotional"
```

**Label prefix is configurable:**
```yaml
# rules.yaml
version: 1
label_prefix: "Cortex"  # prepended to all labels

chains:
  main:
    - match: { from_glob: "*@github.com" }
      action:
        label: "Automated/GitHub"  # becomes Cortex/Automated/GitHub
```

### Reprocessing Modes

The triage worker supports multiple modes:

```bash
# Normal: process new emails from queue
triage-worker

# Reprocess: re-evaluate emails missing classification
triage-worker --reprocess-unclassified

# Reprocess: re-evaluate emails with old rules version
triage-worker --reprocess-rules-version v1

# Reprocess: re-evaluate specific emails
triage-worker --reprocess-gmail-ids abc123,def456

# Reprocess: re-evaluate emails with a specific label (for category refinement)
triage-worker --reprocess-label "Commercial"
triage-worker --reprocess-label "Commercial/%"  # all sub-labels too

# Dry run: evaluate but don't execute actions or save
triage-worker --reprocess-unclassified --dry-run
```

**Reprocessing behavior:**
- Creates new classification row (doesn't update existing)
- By default, executes actions (labels, workflows)
- `--skip-actions` flag to classify without executing
- `--dry-run` to evaluate and log without saving or executing

**Query: Emails with label prefix (for reprocessing)**
```sql
WITH latest_classifications AS (
    SELECT DISTINCT ON (email_id) *
    FROM classifications
    ORDER BY email_id, classified_at DESC
)
SELECT e.id, e.gmail_id
FROM emails_raw e
JOIN latest_classifications lc ON e.id = lc.email_id
WHERE (lc.action_taken->>'label') LIKE 'Cortex/Commercial%';
```

## Workflows

### archiver

Separate service (cortex/hoard) that:
1. Consumes from `archiver` queue
2. Extracts URLs from email body
3. Sends to ArchiveBox for archival
4. Generates summary via LLM
5. Creates Obsidian note

### n8n Webhooks

Actions can POST to n8n webhooks for flexible automation:
```yaml
action:
  webhook: "n8n/todo-from-email"
```

Payload sent:
```json
{
  "gmail_id": "abc123",
  "email_id": 456,
  "from": "devon.jones@gmail.com",
  "subject": "todo: fix the thing",
  "matched_chain": "self_note",
  "matched_rule": 1
}
```

## LLM Classification

When rules jump to `llm_classify` chain, the worker:

1. Fetches body preview from DuckDB API
2. Calls Ollama with classification prompt
3. Maps LLM category to action via `routes` config

**Classification Prompt (v1)**:
```
You are categorizing emails for Devon Jones's personal Gmail inbox.

Categories:
- automated_noise: GitHub notifications, service alerts, social media, marketing
- human_request: Personal emails requiring response (friends, family, colleagues)
- action_item: Has deadline (invoices, permission slips, renewals, bills)
- wrong_email: Misdirected mail (other Devon Joneses in other states)
- subscription: Newsletters, marketing, recurring service emails
- school: Kids' school communications

Context:
- Personal Gmail since ~2004, gets wrong-email constantly
- Two daughters: Aurelia, Anastasia
- Landlord (Real Property Management)
- Works at Valid Eval

Email:
From: {from_addr}
Subject: {subject}
Body preview: {body_preview}

Respond with JSON only:
{
  "category": "...",
  "confidence": 0.0-1.0,
  "reasoning": "brief explanation"
}
```

## Error Handling

**All-or-nothing chain evaluation:**
- Each email is processed as a single transaction
- If any step fails (Ollama down, Gmail API error), the entire evaluation fails
- Failed jobs go to dead letter (status='failed' with error message)
- No partial state: either fully classified + action executed, or nothing

**Dead letter handling:**
```sql
-- Find failed jobs
SELECT * FROM queue
WHERE queue_name = 'triage' AND status = 'failed'
ORDER BY created_at DESC;

-- Retry failed jobs
UPDATE queue SET status = 'pending', attempts = 0, last_error = NULL
WHERE queue_name = 'triage' AND status = 'failed';
```

**Throttling:**
- Queue consumption is naturally throttled by worker poll interval
- Configurable batch size limits concurrent processing
- Gmail API quota: 250 quota units/second (label modify = 5 units)
- Safe default: process 10 emails/second max

## Metrics & Observability

**Key metrics to track:**

| Metric | Description |
|--------|-------------|
| `triage_processed_total` | Emails processed (by chain, action) |
| `triage_llm_calls_total` | LLM invocations (by model, intent) |
| `triage_llm_latency_seconds` | LLM response time histogram |
| `triage_gmail_api_calls_total` | Gmail API calls (by operation) |
| `triage_corrections_total` | User label corrections |
| `triage_queue_depth` | Pending items in triage queue |
| `triage_dead_letter_count` | Failed jobs |

**Logging:**
- Structured JSON logs (structlog)
- Log each classification with: gmail_id, matched_chain, matched_rule, action, latency
- Log LLM calls with: model, prompt_version, response, latency

## Docker Deployment

```yaml
services:
  triage-worker:
    image: us-central1-docker.pkg.dev/cortex-gmail/cortex/triage:latest
    container_name: cortex-triage-worker
    restart: unless-stopped
    environment:
      - POSTGRES_HOST=${POSTGRES_HOST}
      - POSTGRES_DB=${POSTGRES_DB:-cortex}
      - POSTGRES_USER=${POSTGRES_USER:-cortex}
      - POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
      - OLLAMA_URL=${OLLAMA_URL}
      - DUCKDB_API_URL=${DUCKDB_API_URL}
      - GMAIL_TOKEN_PATH=/app/secrets/gmail-token.json
      - RULES_PATH=/app/config/rules.yaml
      - N8N_BASE_URL=${N8N_BASE_URL}
    volumes:
      - ./secrets:/app/secrets:ro
      - ./config:/app/config:ro

  labeling-worker:
    image: us-central1-docker.pkg.dev/cortex-gmail/cortex/triage:latest
    container_name: cortex-labeling-worker
    command: ["uv", "run", "python", "-m", "triage.services.labeling_worker"]
    restart: unless-stopped
    environment:
      - POSTGRES_HOST=${POSTGRES_HOST}
      - POSTGRES_DB=${POSTGRES_DB:-cortex}
      - POSTGRES_USER=${POSTGRES_USER:-cortex}
      - POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
      - GMAIL_TOKEN_PATH=/app/secrets/gmail-token.json
    volumes:
      - ./secrets:/app/secrets:ro
```

Environment variables are configured in Portainer stack settings.

## MVP Scope

Phase 1 (MVP):
- [x] Rules engine with YAML config
- [x] Match conditions: from, to, subject_contains, from_glob
- [x] Actions: label, archive, mark_read, star, workflow
- [x] self_note chain → archiver workflow
- [x] LLM fallback chain
- [x] Compound conditions (any_of, all_of)
- [x] List fields (OR within field)
- [x] Gmail API integration for labels (labeling-worker)

Phase 2:
- [ ] n8n webhook actions
- [ ] digest generation (daily/weekly Obsidian notes)
- [ ] Web UI for rule management
- [ ] Postgres rules storage (multi-user)
- [x] Queue cleanup job (partition maintenance via cortex-utils)

### Gmail Filter Parity Checklist

Synapse should support everything Gmail filters can do, plus more. This checklist tracks parity with Gmail's native filtering capabilities.

**Sender/Recipient Matching:**
- [x] `from` - Exact sender address
- [x] `from_glob` - Glob pattern (e.g., `*@github.com`)
- [x] `from_contains` - Substring in sender
- [x] `to` - Exact recipient address
- [x] `to_glob` - Glob pattern for recipient
- [x] `to_contains` - Substring in recipient
- [ ] `cc` - CC field matching
- [ ] `bcc` - BCC field matching (Gmail: `bcc:me`)
- [ ] `deliveredto` - Delivered-to header (useful for aliases)
- [x] `list` - Mailing list header (List-ID) - via `header: {List-ID: pattern}`
- [x] `replyto` - Reply-To header matching - via `header: {Reply-To: pattern}`

**Content Matching:**
- [x] `subject_contains` - Substring in subject
- [x] `subject_regex` - Regex pattern for subject
- [x] `body_contains` - Substring in body
- [ ] `body_regex` - Regex pattern for body
- [ ] Exact phrase matching (Gmail: `"exact phrase"`)
- [ ] Word proximity (Gmail: `AROUND n`)
- [ ] Negation (Gmail: `-term` to exclude)

**Attachment Matching:**
- [x] `has_attachment` - Boolean has/doesn't have
- [ ] `filename` - Attachment filename pattern (e.g., `*.pdf`)
- [ ] `filename_regex` - Regex for attachment name
- [ ] `has_drive` - Has Google Drive attachment
- [ ] `has_document` - Has Google Docs
- [ ] `has_spreadsheet` - Has Google Sheets
- [ ] `has_presentation` - Has Google Slides
- [ ] `has_youtube` - Has YouTube link

**Size Matching:**
- [ ] `size` - Exact size in bytes
- [ ] `larger` - Larger than size (e.g., `larger:5M`)
- [ ] `smaller` - Smaller than size

**Date/Time Matching:**
- [ ] `before` - Before date (YYYY/MM/DD)
- [ ] `after` - After date
- [ ] `older_than` - Older than duration (e.g., `older_than:7d`)
- [ ] `newer_than` - Newer than duration

**Status Matching:**
- [ ] `is_read` - Read/unread status
- [ ] `is_starred` - Starred status
- [ ] `is_important` - Gmail importance marker
- [ ] `is_muted` - Muted conversations
- [ ] `in_inbox` - Is in inbox
- [ ] `in_spam` - Is in spam
- [ ] `in_trash` - Is in trash
- [ ] `has_label` - Has specific label
- [ ] `has_userlabels` - Has any user label
- [ ] `has_nouserlabels` - Has no user labels

**Gmail Categories:**
- [ ] `category_primary` - Primary tab
- [ ] `category_social` - Social tab
- [ ] `category_promotions` - Promotions tab
- [ ] `category_updates` - Updates tab
- [ ] `category_forums` - Forums tab

**Actions (Synapse has more than Gmail!):**
- [x] `label` - Apply label
- [x] `archive` - Archive (remove from inbox)
- [x] `mark_read` - Mark as read
- [x] `star` - Star the email
- [ ] `mark_important` - Mark as important
- [ ] `never_important` - Never mark important
- [ ] `categorize` - Move to Gmail category
- [ ] `forward` - Forward to address
- [ ] `delete` - Move to trash
- [ ] `never_spam` - Never send to spam

**Synapse-only Features (beyond Gmail):**
- [x] `any_of` / `all_of` - Compound conditions
- [x] List fields - OR within a single field
- [x] `jump` - Chain delegation
- [x] `subject_intent` - LLM-based intent matching
- [x] LLM classification with routes
- [x] Regex patterns on any field
- [x] Built-in intents (archive_request, todo_request, question)
- [x] Generic `header` matching - glob/substring on any RFC822 header

## Reliability Checklist

### Critical (Data Loss/Correctness)

- [ ] **Gmail API rate limiting** - Add token bucket rate limiter for label operations (250 units/sec quota, label modify = 5 units, safe = ~50/sec)
- [ ] **429 error handling** - Add exponential backoff when Gmail API returns rate limit errors
- [ ] **Duplicate classification prevention** - Add idempotency check before inserting classification (prevent duplicates on job retry)
- [ ] **Failed job retry limits** - Add max_attempts enforcement and auto-expiry for dead letter jobs

### High Priority

- [ ] **Explicit LLM failure handling** - Make LLM errors fail the job (or retry) instead of silent "no match" return
- [ ] **Label cache race condition** - Fix concurrent label creation attempts when cache is cleared mid-operation
- [ ] **Partial batch failure handling** - Don't fail entire batch when single email has permission error
- [ ] **Transaction rollback on exception** - Add explicit rollback in exception handlers (don't rely on implicit behavior)
- [ ] **Visibility timeout heartbeat** - Extend claimed_at for long-running jobs to prevent premature reclaim

### Medium Priority

- [ ] **Graceful shutdown** - Wait for in-flight jobs before exit, don't leave jobs in "processing" state
- [ ] **Connection pool sizing** - Review pool size (currently minconn=1, maxconn=5) for expected load
- [ ] **Regex pattern caching** - Pre-compile regex patterns instead of recompiling on every evaluation
- [ ] **LLM confidence bounds** - Validate confidence is 0.0-1.0 before storing (reject NaN, infinity)
- [ ] **Category substring matching** - Fix potential wrong category match when LLM response contains multiple keywords

### Observability

- [ ] **Prometheus metrics** - Add metrics for: `triage_processed_total`, `triage_llm_calls_total`, `triage_llm_latency_seconds`, `triage_gmail_api_errors_total`, `triage_queue_depth`
- [ ] **Consistent structured logging** - Migrate worker.py from stdlib logging to structlog (matcher.py already uses structlog)
- [x] **Discord exception reporting** - Alerter daemon monitors container logs, sends critical errors to Discord (cortex-utils)
- [ ] **Classification audit logging** - Log gmail_id, matched_chain, matched_rule, action, latency for each classification

### Operational

- [x] **Queue partitioning** - Migrated to daily partitioned queue table (cortex-utils)
- [x] **Dead letter table** - Failed jobs preserved in dead_letter table before partition drop
- [x] **Partition maintenance cron** - Daily job via Ofelia at 2 AM (cortex-utils)
- [ ] **Dead letter alerting** - Alert when failed jobs exceed threshold
- [ ] **Label correction tracking** - Implement `label_corrections` table (defined in spec but not implemented)
- [ ] **Rules version tracking** - Store rules file hash in classifications for reprocessing detection

## Testing Strategy

### Unit Tests
```python
def test_match_from_glob():
    rule = {"match": {"from_glob": "*@github.com"}}
    email = {"from_addr": "notifications@github.com"}
    assert matches(rule, email)

def test_chain_jump():
    rules = load_rules("test_rules.yaml")
    email = {"from_addr": "me@me.com", "to_addr": "me@me.com", "subject": "archive this"}
    result = evaluate_chain(rules, "main", email)
    assert result.action == {"workflow": "archiver"}

def test_llm_fallback():
    # Mock Ollama
    rules = load_rules("test_rules.yaml")
    email = {"from_addr": "unknown@random.com", ...}
    result = evaluate_chain(rules, "main", email)
    assert result.chain == "llm_classify"
```

### Integration Tests
- Queue mechanics (SKIP LOCKED)
- Gmail label application
- Ollama classification (with mocked responses)

## Postmark Integration

Triage depends on postmark for queue events. Required changes to postmark:

### Queue Rename: classify → triage

```sql
-- One-time migration
UPDATE queue SET queue_name = 'triage' WHERE queue_name = 'classify';
```

Update `parse_worker.py` to enqueue to `triage`:
```python
# Before
db.enqueue('classify', {'email_id': email_id, 'gmail_id': gmail_id})

# After
db.enqueue('triage', {'email_id': email_id, 'gmail_id': gmail_id})
```

### Label Change Events

Gmail-sync needs to handle `labelAdded`/`labelRemoved` history events for correction detection:

```python
# In gmail_sync.py, when processing history
for record in history:
    if 'labelsAdded' in record:
        for label_added in record['labelsAdded']:
            gmail_id = label_added['message']['id']
            label_ids = label_added['labelIds']
            # Resolve label IDs to names, check for Cortex/* labels
            # Enqueue label change event for triage to process
            db.enqueue('label_change', {
                'gmail_id': gmail_id,
                'label_ids': label_ids,
                'event_type': 'added'
            })

    if 'labelsRemoved' in record:
        # Similar handling
```

Triage worker handles `label_change` queue to detect corrections (see Label Change Detection section).

## Related Documentation

- [Cortex Overview](../../docs/cortex-overview.md)
- [MVP Specification](../../docs/mvp-spec-phase1.md)
- [Postmark Spec](../../postmark/docs/project-spec.md)
