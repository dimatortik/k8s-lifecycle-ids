# controller

Go Pod reconciler (controller-runtime v0.23.3). It watches protected pods and
sets a per-pod anomaly threshold in the ml-service:

| Pod state | Action |
|---|---|
| Pending, or Running and not Ready | `PATCH /threshold {pod_uid, value: θ_high}` |
| every container Ready | clear after `--warmup-ttl` |
| terminating | clear now |

It also emits `ThresholdRaised` / `ThresholdCleared` Kubernetes Events and
serves Prometheus metrics on `:9090`. See [docs/architecture.md](../docs/architecture.md).

## Flags

| Flag | Env | Default | Campaign value |
|---|---|---|---|
| `--ml-service-url` | `ML_SERVICE_URL` | `http://localhost:8080` | `http://ml-service.ids-system.svc.cluster.local:8080` |
| `--pod-selector` | `POD_SELECTOR` | `ids-research.io/protected=true` | same |
| `--high-threshold` | | `-0.95` | `-0.95` |
| `--warmup-ttl` | | `60s` | `60s` |

## Build and run

```bash
go build ./...
go vet ./...
go run ./cmd --ml-service-url http://localhost:8080 --pod-selector ids-research.io/protected=true
```

The image is built by `scripts/infra/build_and_push.sh`; the in-cluster
manifest is `k8s/ids-controller/deployment.yaml`.
