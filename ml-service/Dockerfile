# syntax=docker/dockerfile:1.7

ARG PYTHON_VERSION=3.13

FROM python:${PYTHON_VERSION}-slim AS builder
WORKDIR /build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN --mount=type=cache,target=/var/cache/apt \
    --mount=type=cache,target=/var/lib/apt \
    apt-get update && apt-get install -y --no-install-recommends \
        build-essential

COPY ml-service/requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip wheel --wheel-dir=/wheels -r requirements.txt

FROM python:${PYTHON_VERSION}-slim AS runtime
WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN groupadd --gid 10001 app && \
    useradd --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app

COPY --from=builder /wheels /wheels
COPY ml-service/requirements.txt /tmp/requirements.txt
RUN pip install --no-index --find-links=/wheels -r /tmp/requirements.txt \
    && rm -rf /wheels /tmp/requirements.txt

COPY ml-service/app/ /app/app/

# Build from the repo root. The trained models are baked into the image;
# models_dict.py resolves /models relative to /app/app.
COPY --chown=app:app models/baseline.pkl /models/baseline.pkl
COPY --chown=app:app models/onlineboutique/ /models/onlineboutique/
RUN chown -R app:app /models

USER app:app

EXPOSE 8080

# Exactly one worker: threshold state lives in process memory.
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8080", \
     "--workers", "1", \
     "--log-level", "info"]
