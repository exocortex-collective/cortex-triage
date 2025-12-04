FROM python:3.11-slim

WORKDIR /app

# Install uv
RUN pip install uv

# Copy everything needed for build
COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/

# Install dependencies (includes building the package)
RUN uv sync --frozen --no-dev

# Secrets will be mounted at runtime
VOLUME /app/secrets

# Run the triage worker service (placeholder - update when service is built)
CMD ["uv", "run", "python", "-m", "triage.services.worker"]
