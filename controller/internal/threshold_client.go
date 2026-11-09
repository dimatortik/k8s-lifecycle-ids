package internal

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"time"

	"sigs.k8s.io/controller-runtime/pkg/log"
)

// serverTTLMultiplier makes the ml-service TTL of a raise longer than the
// controller's post-Ready hold, so the server-side expiry never fires first.
const serverTTLMultiplier = 5

// thresholdPayload mirrors ThresholdUpdate in ml-service/app/models.py.
type thresholdPayload struct {
	PodUID     string   `json:"pod_uid,omitempty"`
	Namespace  string   `json:"namespace,omitempty"`
	Label      string   `json:"label,omitempty"`
	Value      *float64 `json:"value"`
	TTLSeconds *int     `json:"ttl_seconds,omitempty"`
}

// raisePodThreshold sets a per-pod threshold with a server-side TTL of
// ttl × serverTTLMultiplier.
func (r *PodReconciler) raisePodThreshold(
	ctx context.Context, uid string, value float64, ttl time.Duration,
) error {
	serverTTL := time.Duration(serverTTLMultiplier) * ttl
	ttlSec := int(serverTTL.Seconds())
	return r.sendThresholdPatch(ctx, thresholdPayload{
		PodUID:     uid,
		Value:      &value,
		TTLSeconds: &ttlSec,
	})
}

// clearPodThreshold removes the per-pod threshold (value=null).
func (r *PodReconciler) clearPodThreshold(ctx context.Context, uid string) error {
	return r.sendThresholdPatch(ctx, thresholdPayload{
		PodUID: uid,
		Value:  nil,
	})
}

// sendThresholdPatch PATCHes /threshold, retrying 503 up to three times.
func (r *PodReconciler) sendThresholdPatch(ctx context.Context, payload thresholdPayload) error {
	logger := log.FromContext(ctx)

	body, err := json.Marshal(payload)
	if err != nil {
		return fmt.Errorf("marshal: %w", err)
	}

	url := r.MLServiceURL + "/threshold"
	backoff := 100 * time.Millisecond

	for attempt := 1; attempt <= 3; attempt++ {
		req, err := http.NewRequestWithContext(ctx, http.MethodPatch, url, bytes.NewReader(body))
		if err != nil {
			return fmt.Errorf("build request: %w", err)
		}
		req.Header.Set("Content-Type", "application/json")

		resp, err := r.HTTPClient.Do(req)
		if err != nil {
			return fmt.Errorf("do request: %w", err)
		}
		io.Copy(io.Discard, resp.Body) // drain so the connection can be reused
		resp.Body.Close()

		switch resp.StatusCode {
		case http.StatusOK:
			return nil
		case http.StatusConflict: // an existing entry was overwritten
			logger.Info("ML service: active suppression overwritten",
				"pod_uid", payload.PodUID)
			return nil
		case http.StatusNotFound: // on a clear: the entry is already gone
			if payload.Value == nil {
				logger.V(1).Info("clear: entry already gone",
					"pod_uid", payload.PodUID)
				return nil
			}
			return fmt.Errorf("unexpected HTTP %d from ML service", resp.StatusCode)
		case http.StatusServiceUnavailable:
			if attempt < 3 {
				logger.Info("ML service unavailable, retrying",
					"attempt", attempt, "backoffMs", backoff.Milliseconds())
				select {
				case <-time.After(backoff):
				case <-ctx.Done():
					return ctx.Err()
				}
				backoff *= 2
				continue
			}
			return fmt.Errorf("ML service unavailable after %d attempts", attempt)
		default:
			return fmt.Errorf("unexpected HTTP %d from ML service", resp.StatusCode)
		}
	}
	return nil
}
