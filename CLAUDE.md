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
│   ├── rules.example.yaml  # Example rules (checked in)
│   └── rules.yaml          # Actual rules (NOT checked in - gitignored)
├── tests/
├── docs/
│   └── project-spec.md
├── pyproject.toml
└── Dockerfile
```

## Configuration

**IMPORTANT**: `config/rules.yaml` is gitignored and NOT checked into this repo. It contains personal email addresses and filtering rules.

- **Example config**: `config/rules.example.yaml` shows the structure with generic examples
- **Personal config**: Stored at `~/HomeLab/cortex/triage/rules.yaml` and mounted into containers at runtime
- For local development, copy `rules.example.yaml` to `config/rules.yaml` and customize

## Architecture

Firewall-style rules engine:
- Rules evaluated in order (first match wins)
- Chains for sub-evaluation (like iptables -j CHAIN)
- Actions: label, archive, workflow dispatch, webhook
- LLM fallback for fuzzy matching

## Postgres Access

Postgres runs in a Docker container. To run queries:

```bash
# Via docker exec
ssh <user>@<postgres-host> "docker exec -i cortex-postgres psql -U cortex -d cortex -c 'SELECT ...'"

# Interactive
ssh <user>@<postgres-host> "docker exec -it cortex-postgres psql -U cortex -d cortex"
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
POSTGRES_HOST=<postgres-host>
POSTGRES_PORT=5432
POSTGRES_DB=cortex
POSTGRES_USER=cortex
POSTGRES_PASSWORD=<secret>

# Ollama (for LLM classification and intent matching)
OLLAMA_URL=http://<ollama-host>:11434

# DuckDB API (for body preview)
DUCKDB_API_URL=http://<duckdb-api-host>:8081

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
uv run ruff check .
```

## Git Workflow

**NEVER push directly to main.** Always:

1. Create a feature branch
2. Make changes and commit
3. Push the branch and create a PR
4. Wait for CI and Gemini Code Assist review
5. Merge via GitHub after approval

This applies to all changes, no matter how small.

## Deployment

- Docker image: `us-central1-docker.pkg.dev/cortex-gmail/cortex/triage:latest`
- Stack: `cortex-triage` on Hades (endpoint 11), stack ID 34
- Depends on: Postgres, Ollama, DuckDB API, postmark queue

### Rules Sync

**IMPORTANT**: Before deploying, always sync the rules file from HomeLab to Hades:

```bash
scp ~/HomeLab/cortex/triage/rules.yaml devon@10.5.2.21:/home/devon/cortex-config/rules.yaml
```

This ensures the latest rules are deployed. The rules file is edited locally in HomeLab and mounted into the container at runtime.

## GitHub Integrations

- **Gemini Code Assist**: Automatic code review on PRs
- **Renovate**: Automated dependency updates
