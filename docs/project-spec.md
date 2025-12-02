# Triage - Email Classification & Routing

**Project**: cortex-triage
**Purpose**: LLM-based email classification, Gmail label routing, and digest generation

## Overview

Triage consumes emails from postmark's queue, classifies them using Ollama, applies Gmail labels via the Gmail API, and generates daily/weekly digests to Obsidian.

## Architecture

```
         ┌───────────────┐
         │   postmark    │
         │   (queue)     │
         └───────┬───────┘
                 │ classify queue
                 ▼
         ┌───────────────┐
         │  classifier   │  (Python service)
         │  Ollama calls │  (http://hades:11434)
         └───────┬───────┘
                 │ route queue
                 ▼
         ┌───────────────┐
         │    router     │  (Python service)
         │  Gmail labels │
         └───────┬───────┘
                 │
                 ▼
         ┌───────────────┐
         │    Gmail      │
         │   (labels)    │
         └───────────────┘

         ┌───────────────┐
         │    digest     │  (cron job)
         │   Obsidian    │
         └───────────────┘
```

## Classification Categories

### Primary Categories (Personal Email)

**automated_noise** (archive silently):
- GitHub notifications (PR opened, issue commented, CI passed)
- Service status updates (all green, routine)
- Low-value newsletters
- Marketing emails (unless from important sender)
- Social media notifications

**human_request** (needs response):
- Personal emails from friends/family
- Property manager questions (Real Property Management)
- School teacher emails (not automated alerts)
- Work colleague emails
- Any direct question requiring response

**action_item** (deadline tracking):
- Landlord invoices (due dates)
- School permission slips (deadlines)
- Certificate expiring alerts
- Subscription renewals requiring action
- Bills due
- Calendar events requiring prep/RSVP

**archive_content** (personal knowledge):
- Email from self with URL in body
- Subject contains: "archive", "save", "read later", "clip"
- Research threads

**wrong_email** (auto-respond or archive):
- Misdirected emails (other Devon Joneses in GA, TX, CA, etc.)
- Medical results, legal docs, school communications for wrong person
- High confidence → auto-respond, medium → review, low → archive

**subscription** (newsletter management):
- Newsletters, marketing, recurring service emails
- Track sender for future management

**school** (kids' school communications):
- Field trip announcements, permission slips
- Grade reports, parent-teacher conferences
- Early dismissals, school closures
- Volunteer requests

## Services

### 1. classifier

**Purpose**: Classify emails using Ollama LLM

**Queue**: Consumes `classify`, produces `route`

**Pseudocode**:
```python
while True:
    item = db.dequeue('classify')
    if not item:
        time.sleep(5)
        continue

    email = db.fetch_email(item['payload']['email_id'])
    body_preview = duckdb.fetch_body_preview(item['payload']['gmail_id'], max_chars=500)

    result = ollama.classify(
        from_addr=email['from_addr'],
        subject=email['subject'],
        body_preview=body_preview,
        model='qwen2.5:7b',
        prompt_version='v1'
    )

    db.insert_classification({
        'email_id': email['id'],
        'category': result['category'],
        'confidence': result['confidence'],
        'reasoning': result['reasoning'],
        'model_version': 'qwen2.5:7b',
        'prompt_version': 'v1'
    })

    if result['category'] in ['human_request', 'action_item', 'archive_content']:
        db.enqueue('route', {
            'email_id': email['id'],
            'gmail_id': item['payload']['gmail_id'],
            'category': result['category']
        })

    db.complete_queue_item(item['id'])
```

**Classification Prompt** (v1):
```
You are analyzing emails for Devon Jones to categorize them for his personal Gmail inbox.

Categories:
- automated_noise: GitHub notifications, service status updates, social media notifications, low-value newsletters
- human_request: Personal emails from friends/family, property manager questions, school teacher emails, work colleagues - anything requiring response
- action_item: Landlord invoices with due dates, school permission slips, cert expiring, bills due, subscription renewals needing action
- archive_content: Email from Devon to himself with URL to save for later research
- wrong_email: Misdirected mail (medical results, legal docs for other Devon Jones in other states)
- subscription: Newsletters, marketing, recurring service emails
- school: Kids' school communications (field trips, grade reports, early dismissals)

Context about Devon:
- Personal Gmail (devon.jones@gmail.com) since ~2004, gets wrong-email constantly
- Has two daughters in school (Aurelia, Anastasia)
- Landlord with rental properties (Real Property Management communications)
- Broad interests: gaming, 3D printing, homelab, parenting, tech
- Works at Valid Eval (some work email to personal inbox)

Email:
From: {from_addr}
Subject: {subject}
Body preview: {body_preview}

Respond with JSON only:
{
  "category": "automated_noise|human_request|action_item|archive_content|wrong_email|subscription|school",
  "confidence": 0.0-1.0,
  "reasoning": "brief explanation (1 sentence)"
}
```

### 2. router

**Purpose**: Apply Gmail labels based on classification

**Queue**: Consumes `route`

**Pseudocode**:
```python
while True:
    item = db.dequeue('route')
    if not item:
        time.sleep(5)
        continue

    gmail_id = item['payload']['gmail_id']
    category = item['payload']['category']

    if category == 'automated_noise':
        gmail.apply_label(gmail_id, 'Automated - Archived')
        gmail.archive(gmail_id)
        gmail.mark_read(gmail_id)

    elif category == 'human_request':
        gmail.apply_label(gmail_id, 'Human - Needs Response')
        gmail.star(gmail_id)

    elif category == 'action_item':
        gmail.apply_label(gmail_id, 'Action Required')
        gmail.star(gmail_id)

    elif category == 'wrong_email':
        gmail.apply_label(gmail_id, 'Wrong Email')
        gmail.archive(gmail_id)

    elif category == 'subscription':
        gmail.apply_label(gmail_id, 'Subscription')

    elif category == 'school':
        gmail.apply_label(gmail_id, 'School')

    db.complete_queue_item(item['id'])
```

### 3. digest (cron job)

**Purpose**: Generate daily/weekly review in Obsidian

**Schedule**: Daily at 7 AM, weekly on Friday

**Output**: Markdown file in Obsidian vault with:
- Action items grouped by type (landlord, school, other)
- Human requests needing response
- Stats (emails processed, automated archived, etc.)
- Links to Gmail for each email

## Data Model

### Classifications Table

```sql
CREATE TABLE classifications (
    id BIGSERIAL PRIMARY KEY,
    email_id BIGINT NOT NULL REFERENCES emails(id),
    category TEXT NOT NULL,
    confidence REAL NOT NULL,
    reasoning TEXT,
    model_version TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    classified_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_classifications_email ON classifications(email_id);
CREATE INDEX idx_classifications_category ON classifications(category);
CREATE INDEX idx_classifications_date ON classifications(classified_at DESC);
```

## Docker Deployment

### Stack: cortex-triage

```yaml
version: '3.8'
services:
  classifier:
    image: ghcr.io/devonjones/cortex-triage:latest
    container_name: cortex-triage-classifier
    command: python -m triage.services.classifier
    restart: unless-stopped
    environment:
      - POSTGRES_HOST=10.5.2.21
      - POSTGRES_PORT=5432
      - POSTGRES_DB=cortex
      - POSTGRES_USER=cortex
      - POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
      - OLLAMA_URL=http://10.5.2.21:11434
      - OLLAMA_MODEL=qwen2.5:7b
      - DUCKDB_PATH=/app/data/emails.duckdb
    volumes:
      - postmark-data:/app/data:ro
    networks:
      - cortex

  router:
    image: ghcr.io/devonjones/cortex-triage:latest
    container_name: cortex-triage-router
    command: python -m triage.services.router
    restart: unless-stopped
    environment:
      - POSTGRES_HOST=10.5.2.21
      - POSTGRES_PORT=5432
      - POSTGRES_DB=cortex
      - POSTGRES_USER=cortex
      - POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
      - GMAIL_TOKEN_PATH=/app/secrets/gmail-token.json
    volumes:
      - /home/devon/.cortex/gmail-token.json:/app/secrets/gmail-token.json:ro
    networks:
      - cortex

networks:
  cortex:
    external: true

volumes:
  postmark-data:
    external: true
```

## Prompt Versioning

Prompts stored in `src/triage/prompts/` with version tracking:
- `v1.txt` - Initial prompt
- `v2.txt` - After first round of misclassification review
- etc.

Classification records store `prompt_version` for reproducibility and reprocessing.

## Testing Strategy

### Unit Tests
```python
def test_classify_github_notification():
    result = classify_email(
        from_addr="notifications@github.com",
        subject="[user/repo] PR #123 opened",
        body="..."
    )
    assert result['category'] == 'automated_noise'
    assert result['confidence'] > 0.8

def test_classify_human_request():
    result = classify_email(
        from_addr="friend@example.com",
        subject="Coffee next week?",
        body="..."
    )
    assert result['category'] == 'human_request'
```

### Integration Tests
- Mock Ollama responses
- Test queue mechanics (SKIP LOCKED)
- Test Gmail label application

## Related Documentation

- [Cortex Overview](../../docs/cortex-overview.md)
- [MVP Specification](../../docs/mvp-spec-phase1.md)
- [Postmark Spec](../postmark/docs/project-spec.md)
