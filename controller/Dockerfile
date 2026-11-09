# syntax=docker/dockerfile:1.7

ARG GO_VERSION=1.25.1

FROM golang:${GO_VERSION}-alpine AS builder
WORKDIR /workspace

COPY go.mod go.sum ./
RUN --mount=type=cache,target=/go/pkg/mod \
    --mount=type=cache,target=/root/.cache/go-build \
    go mod download

COPY cmd/      cmd/
COPY internal/ internal/

ENV CGO_ENABLED=0 GOOS=linux
RUN --mount=type=cache,target=/go/pkg/mod \
    --mount=type=cache,target=/root/.cache/go-build \
    go build -trimpath -ldflags="-s -w" -o /workspace/manager ./cmd

FROM gcr.io/distroless/static:nonroot
WORKDIR /
COPY --from=builder /workspace/manager /manager

USER 65532:65532

# Prometheus metrics.
EXPOSE 9090

ENTRYPOINT ["/manager"]
