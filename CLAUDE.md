# Triage - Email Classification & Routing

Rule-based email classification and workflow dispatch, with LLM fallback.

## Quick Reference

- **Spec**: `docs/project-spec.md` (full specification)
- **Services**: `src/triage/services/worker.py` (main triage worker)
- **Consumes**: Queue from postmark (`triage` queue)
- **Produces**: Gmail labels (`Cortex/*`), workflow dispatches

## Project Structure

```
triage/
├── src/triage/
│   ├── services/
│   │   └── worker.py       # Main triage worker
│   ├── engine/
│   │   ├── rules.py        # Rule loading and evaluation
│   │   ├── matcher.py      # Match condition evaluation
│   │   └── actions.py      # Action execution
│   ├── intents/            # Built-in intent prompts
│   └── models/             # Data models
├── config/
│   └── rules.yaml          # Rules configuration
├── tests/
├── docs/
│   └── project-spec.md
├── pyproject.toml
└── Dockerfile
```

## Architecture

Firewall-style rules engine:
- Rules evaluated in order (first match wins)
- Chains for sub-evaluation (like iptables -j CHAIN)
- Actions: label, archive, workflow dispatch, webhook
- LLM fallback for fuzzy matching

## Postgres Access

Postgres runs in a Docker container on hades. To run queries:

```bash
# Via docker exec
ssh devon@10.5.2.21 "docker exec -i cortex-postgres psql -U cortex -d cortex -c 'SELECT ...'"

# Interactive
ssh devon@10.5.2.21 "docker exec -it cortex-postgres psql -U cortex -d cortex"
```

## Queue Interface

Consumes from postmark:
```sql
-- Dequeue pattern (SKIP LOCKED)
SELECT * FROM queue
WHERE queue_name = 'triage' AND status = 'pending'
ORDER BY created_at
LIMIT 1
FOR UPDATE SKIP LOCKED;
```

## Environment Variables

```bash
# Postgres (shared with postmark)
POSTGRES_HOST=10.5.2.21
POSTGRES_PORT=5432
POSTGRES_DB=cortex
POSTGRES_USER=cortex
POSTGRES_PASSWORD=<secret>

# Ollama (for LLM classification and intent matching)
OLLAMA_URL=http://10.5.2.21:11434

# DuckDB API (for body preview)
DUCKDB_API_URL=http://duckdb-api:8081

# Gmail API (for labels)
GMAIL_TOKEN_PATH=/app/secrets/gmail-token.json

# Rules
RULES_PATH=/app/config/rules.yaml
```

## Development

```bash
uv sync
uv run pytest
uv run mypy src/triage
uv run black .
uv run isort .
```

## Deployment

- Docker image: `us-central1-docker.pkg.dev/cortex-gmail/cortex/triage:latest`
- Stack: `cortex-triage` on Hades (endpoint 11)
- Depends on: Postgres, Ollama, DuckDB API, postmark queue

## GitHub Integrations

- **Gemini Code Assist**: Automatic code review on PRs
- **Renovate**: Automated dependency updates
