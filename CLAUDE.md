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

**The worker loads config from Postgres, not from disk.** `RULES_PATH`,
`config/rules.yaml` and the `~/HomeLab/cortex/triage/rules.yaml` mount are all
dead — nothing reads them. `load_config_from_db()` (cortex-utils) reads the row
in `triage_config_versions` where `is_active = TRUE` and rebuilds the chains
from `triage_chains` / `triage_rules`.

- **Chains and rules** are edited by round-tripping YAML through the Gateway
  `/config` API. There is no CRUD API for rules; the YAML is the interface.
- **Priority / fallback email mappings** live in `triage_email_mappings`, are
  NOT config-versioned, and are edited through the Gateway `/mappings` CRUD API.
  Do not manage them through the `/config` YAML — the importer is upsert-only,
  so deletions there are silently ignored (cortex-ac56).
- `config/rules.example.yaml` is still useful as a schema reference.

Full workflow, hazards, and the convergence/backfill behaviour:
`~/Projects/cortex/CLAUDE.md` → "Triage Config Management".

## Architecture

Firewall-style rules engine, but **parallel**, not single-pass:

- Every chain with `auto_run: true` is evaluated independently against every
  email (`evaluate_parallel` in `src/triage/engine/matcher.py`). `main` defaults
  to `auto_run: true`; every other chain defaults to `false` and is reachable
  only via `jump`.
- Within one chain, rules are evaluated in order and the first match wins.
- The per-chain results are merged with CRDT semantics
  (`src/triage/engine/merge.py`) — see "Writing rules for parallel chains" below.
- Priority email mappings participate in the merge as a virtual chain. Fallback
  mappings, then `default_label`, fire only if nothing produced a label.
- LLM intent/classification for fuzzy matching, configured per-rule.

`evaluate()` (the old serial, first-match-wins, priority-short-circuits path)
still exists in `matcher.py` but **the worker does not call it**. Don't reason
about production behaviour from it.

### Writing rules for parallel chains

Because every `auto_run` chain gets a vote, an action set by one chain can be
cancelled by another:

| Field | Merge rule |
|-------|-----------|
| `label`, `add_label`, `remove_label` | **Union.** Purely additive — an email can end up with labels from several chains. |
| `archive`, `mark_read` | **Conservative — `all()` of the chains that have an opinion.** One dissenting chain suppresses it. |
| `star`, `important` | **Aggressive — `any()`.** One chain asserting it wins. |
| `force_archive`, `force_mark_read`, `force_no_star`, `force_no_important` | **Vetoes.** Applied last, override the merged value unconditionally. |

A chain expresses "no opinion" by leaving the field unset (`None`), which is why
`exclude_none` matters and why `false` is *not* the same as omitted.

**Rule of thumb: an additive "scan" chain must set only `add_label` and say
nothing about `archive` / `mark_read`.** Setting `archive: false` in a scan chain
is not a no-op — it is a vote, and it will stop another chain from archiving the
email. Use `force_archive` only when you genuinely mean to override every other
chain.

### Convergence (automatic backfill on rule change)

`classifications.config_version_id` tracks which config version labelled each
email. When the triage queue is idle the worker picks the **newest** email
behind the active version and re-triages it in-process, one per poll, walking
backwards through history (`_converge_priority_and_get_next_email`). Emails from
priority-mapped senders are bulk-updated in place every 5 minutes instead.

So **publishing a new config version silently re-labels historical mail.** No
manual re-enqueue is needed after a rules change. Disable with
`ENABLE_CONVERGENCE=false`; check progress with
`GET /triage/convergence/status` on the gateway.

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

# Convergence (idle-time re-triage of emails on an older config version)
ENABLE_CONVERGENCE=true   # default

# NOTE: there is no RULES_PATH. Config comes from Postgres.
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

**Not a thing any more.** Rules live in Postgres and survive redeploys; there is
no file to scp and no volume to mount. Change rules with `PUT /config` on the
gateway (see `~/Projects/cortex/CLAUDE.md` → "Triage Config Management").

## GitHub Integrations

- **Gemini Code Assist**: Automatic code review on PRs
- **Renovate**: Automated dependency updates
