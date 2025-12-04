# Triage - Email Classification & Routing

Email classification, rule-based routing, and workflow dispatch for Cortex.

## Overview

Triage consumes emails from postmark's `classify` queue and routes them to appropriate workflows:
- **Deterministic rules**: Fast, cheap matching on email attributes (sender, recipient, subject patterns)
- **LLM classifier**: Fuzzy matching via Ollama for complex cases
- **Workflow routing**: Dispatches to archiver, n8n, or applies Gmail labels

## Quick Start

```bash
uv sync
uv run pytest
uv run mypy src/triage
```

See `docs/project-spec.md` for full specification.
