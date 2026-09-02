FROM python:3.11-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai

RUN apt-get update && apt-get install -y --no-install-recommends tzdata && rm -rf /var/lib/apt/lists/*

# Step 1: Install dependencies only (cached unless pyproject.toml changes).
# This layer survives source-code edits, so pip doesn't re-download on every build.
COPY pyproject.toml ./
RUN pip install --no-cache-dir \
    fastapi>=0.110 \
    "uvicorn[standard]>=0.27" \
    httpx>=0.27 \
    "sqlalchemy>=2.0" \
    aiosqlite>=0.20 \
    "apscheduler>=3.10" \
    "tenacity>=8.2"

# Step 2: Copy source code (changes often, but deps are already cached).
COPY backend/ backend/
COPY frontend/ frontend/
COPY scripts/ scripts/

# Ensure backend package is importable without editable install.
ENV PYTHONPATH=/app

RUN mkdir -p data

EXPOSE 8000

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
