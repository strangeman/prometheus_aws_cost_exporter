# ---------- builder: resolve and install dependencies into /app/.venv ----------
FROM python:3.14-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_CREATE=true \
    POETRY_VIRTUALENVS_IN_PROJECT=true

RUN pip install "poetry==2.4.1"

WORKDIR /app

COPY pyproject.toml poetry.lock ./

# --no-root: the application code is not copied yet (and is not installed as a
# package at runtime — it is imported from /app).
RUN poetry install --only main --no-root

# ---------- runtime ----------
FROM python:3.14-slim AS runtime

# Use a less-privileged user for the runtime image
RUN adduser --shell /bin/bash --home /app --disabled-password --gecos "" appuser

WORKDIR /app

ENV FLASK_APP=app/app.py \
    PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ="Etc/GMT+3"

COPY --from=builder --chown=appuser:appuser /app/.venv /app/.venv
COPY --chown=appuser:appuser app ./app

USER appuser

# Expose application's port
EXPOSE 5000

# Command to run the application
CMD ["flask", "run", "--host", "0.0.0.0"]
