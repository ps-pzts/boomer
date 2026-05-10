FROM python:3.11-slim

WORKDIR /app

# Install deps in a separate layer so source changes don't bust the cache
COPY pyproject.toml ./
RUN pip install --no-cache-dir -e "." 2>/dev/null || true
# Re-install after source is copied (editable install needs the src tree)
COPY src/ ./src/
COPY migrations/ ./migrations/
RUN pip install --no-cache-dir -e "."

# Runtime directories (overridden by volume mount in production)
RUN mkdir -p /var/lib/boomer/archive /var/lib/boomer/backups /var/log/boomer

ENV BOOMER_DB_PATH=/var/lib/boomer/boomer.db
ENV BOOMER_ARCHIVE_DIR=/var/lib/boomer/archive
ENV BOOMER_BACKUP_DIR=/var/lib/boomer/backups
ENV PYTHONPATH=/app

EXPOSE 8080

# Default: dashboard. Override CMD to run the orchestrator.
CMD ["uvicorn", "src.dashboard.app:app", "--host", "0.0.0.0", "--port", "8080"]
