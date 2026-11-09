# ml-service

FastAPI scoring service. One IsolationForest per Online Boutique service plus
a fleet-pooled fallback (`baseline.pkl`); per-pod and per-label threshold
overrides with TTL. Threshold selection and endpoints are described in
[docs/architecture.md](../docs/architecture.md).

## Run locally

The trained models come from the data release (`make data`, see the root README).

```bash
pip install -r requirements.txt
IDS_MODELS_DIR=../models uvicorn app.main:app --port 8080 --workers 1
curl -s localhost:8080/health
```

It must run as a single worker: threshold state is kept in process memory.

## Tests

```bash
IDS_MODELS_DIR=../models python -m pytest -q app/test_api_smoke.py
```

## Image

`Dockerfile` (build context = repository root) bakes the models from
`models/` into the image. Built and pushed by `scripts/infra/build_and_push.sh`.
