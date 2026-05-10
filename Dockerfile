FROM python:3.11-slim

WORKDIR /app

# System deps for torch + kiteconnect websocket
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ libssl-dev && \
    rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
RUN pip install --no-cache-dir -e ".[dev]"

COPY migrations/ ./migrations/
COPY src/ ./src/
COPY ops/ ./ops/

# Data directories
RUN mkdir -p /var/lib/boomer/archive /var/lib/boomer/backups

ENV BOOMER_DB_PATH=/var/lib/boomer/boomer.db
ENV BOOMER_ARCHIVE_DIR=/var/lib/boomer/archive
ENV BOOMER_BACKUP_DIR=/var/lib/boomer/backups
ENV PYTHONPATH=/app/src

EXPOSE 8000
