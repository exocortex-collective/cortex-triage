FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install uv
RUN pip install uv

# Copy dependency definitions first (for layer caching)
COPY pyproject.toml uv.lock README.md ./

# Create non-root system user (no shell, no home for security)
RUN useradd --system --shell /bin/false appuser

# Install dependencies (before copying source for better caching)
# Need src/ stub for uv sync to work with editable install
COPY src/triage/__init__.py ./src/triage/__init__.py
RUN uv sync --frozen --no-dev

# Copy application source
COPY --chown=appuser:appuser src/ ./src/
COPY --chown=appuser:appuser config/ ./config/

# Config and secrets will be mounted at runtime
VOLUME /app/config
VOLUME /app/secrets

# Switch to non-root user
USER appuser

# Run the triage worker service
CMD ["uv", "run", "python", "-m", "triage.services.worker"]
