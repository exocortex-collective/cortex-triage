# Triage - Email Classification & Routing

Email classification, Gmail label routing, and digest generation for Cortex.

## Quick Reference

- **Spec**: `docs/project-spec.md`
- **Services**: `src/triage/services/` (classifier, router, digest)
- **Consumes**: Queue from postmark (`classify` queue)
- **Produces**: Gmail labels, Obsidian digests

## Project Structure

```
triage/
├── src/triage/
│   ├── services/
│   │   ├── classifier.py    # LLM classification via Ollama
│   │   ├── router.py        # Gmail label application
│   │   └── digest.py        # Daily/weekly digest generation
│   ├── prompts/             # Classification prompts (versioned)
│   └── models/              # Data models
├── tests/
├── docs/
│   └── project-spec.md
├── pyproject.toml
└── Dockerfile
```

## Classification Categories

- **automated_noise**: GitHub notifications, service alerts, low-value newsletters
- **human_request**: Personal emails requiring response
- **action_item**: Deadlines, invoices, permission slips
- **archive_content**: Self-emailed URLs for archival
- **wrong_email**: Misdirected mail (other Devon Joneses)
- **subscription**: Newsletters, marketing
- **school**: Kids' school communications

## Environment Variables

```bash
# Postgres (shared with postmark)
POSTGRES_HOST=10.5.2.21
POSTGRES_PORT=5432
POSTGRES_DB=cortex
POSTGRES_USER=cortex
POSTGRES_PASSWORD=<secret>

# Ollama
OLLAMA_URL=http://10.5.2.21:11434
OLLAMA_MODEL=qwen2.5:7b

# Gmail API (for router)
GMAIL_TOKEN_PATH=/app/secrets/gmail-token.json

# Obsidian (for digests)
OBSIDIAN_REPO_PATH=/app/obsidian
```

## Queue Interface

Consumes from postmark:
```sql
-- Dequeue pattern (SKIP LOCKED)
SELECT * FROM queue
WHERE queue_name = 'classify' AND status = 'pending'
ORDER BY created_at
LIMIT 1
FOR UPDATE SKIP LOCKED;
```

Produces for router:
```sql
INSERT INTO queue (queue_name, payload)
VALUES ('route', '{"email_id": 123, "gmail_id": "abc", "category": "human_request"}');
```

## Development

```bash
uv sync
uv run pytest
uv run mypy src/triage
```

## Deployment

- Docker image: `ghcr.io/devonjones/cortex-triage:latest`
- Stack: `cortex-triage` on Hades (or Ares)
- Depends on: Postgres, Ollama, postmark queue

## GitHub Integrations

- **Gemini Code Assist**: Automatic code review on PRs
- **Renovate**: Automated dependency updates
