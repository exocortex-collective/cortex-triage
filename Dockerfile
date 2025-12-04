FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Create a non-root user
RUN addgroup --system app && adduser --system --ingroup app app

# Install uv
RUN pip install uv

# Copy dependency definitions and install dependencies (layer caching)
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev

# Copy application source
COPY src/ ./src/

# Change ownership of the app directory
RUN chown -R app:app /app

# Secrets will be mounted at runtime
VOLUME /app/secrets

# Switch to the non-root user
USER app

# Run the triage worker service (placeholder - update when service is built)
CMD ["uv", "run", "python", "-m", "triage.services.worker"]
