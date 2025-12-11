FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install uv
RUN pip install uv

# Copy dependency definitions and README (needed for package metadata)
COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/
COPY config/ ./config/

# Install dependencies (includes building the package)
RUN uv sync --frozen --no-dev

# Config and secrets will be mounted at runtime
VOLUME /app/config
VOLUME /app/secrets

# Run the triage worker service
CMD ["uv", "run", "python", "-m", "triage.services.worker"]
