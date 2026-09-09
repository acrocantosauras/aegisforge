FROM python:3.12-slim AS base

# Prevent Python from writing .pyc files and enable unbuffered output
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install system dependencies in a separate layer for caching
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy package metadata first (layer cache: only re-install when deps change)
COPY pyproject.toml README.md ./
COPY src/ src/

# Install application + dependencies (production, not editable)
RUN pip install --no-cache-dir .

# Copy remaining application files
COPY alembic/ alembic/
COPY alembic.ini ./

# Create a non-root user and switch to it
RUN groupadd -r aegisforge && useradd --no-log-init -r -g aegisforge aegisforge \
    && chown -R aegisforge:aegisforge /app
USER aegisforge

EXPOSE 8000

CMD ["uvicorn", "aegisforge.app:app", "--host", "0.0.0.0", "--port", "8000"]
