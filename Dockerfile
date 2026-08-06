FROM python:3.11-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai

RUN apt-get update && apt-get install -y --no-install-recommends tzdata && rm -rf /var/lib/apt/lists/*

# Editable install so backend/ stays at /app/backend and the app can resolve
# frontend/ and data/ relative to the package source.
COPY pyproject.toml ./
COPY backend/ backend/
# Aliyun PyPI mirror — fast in mainland China (works elsewhere too).
RUN pip install --no-cache-dir -e . -i https://mirrors.aliyun.com/pypi/simple/

# Non-package application sources.
COPY frontend/ frontend/
COPY scripts/ scripts/

RUN mkdir -p data

EXPOSE 8000

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
