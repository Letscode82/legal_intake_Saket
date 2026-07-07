# AEGIS FastAPI backend — containerized for Fly.io / Render / Railway.
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install dependencies first for layer caching.
COPY pyproject.toml ./
RUN pip install --upgrade pip && pip install .

# App source.
COPY alembic.ini ./
COPY app ./app

EXPOSE 8000

# Run DB migrations, then serve. In production, prefer running migrations as a
# separate release step; this default keeps single-container deploys simple.
CMD ["sh", "-c", "alembic upgrade head && gunicorn app.main:app -k uvicorn.workers.UvicornWorker -w ${WEB_CONCURRENCY:-2} -b 0.0.0.0:${PORT:-8000}"]
