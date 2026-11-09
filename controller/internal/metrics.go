package internal

import (
	"github.com/prometheus/client_golang/prometheus"
	"sigs.k8s.io/controller-runtime/pkg/metrics"
)

// Metrics are registered on controller-runtime's registry, which is served on
// :9090 together with the Go runtime and process collectors.

// reconcileLatency is T_ctrl: time spent inside Reconcile, including the
// PATCH /threshold round-trip. outcome is raise | clear | restore_scheduled |
// noop | error.
var reconcileLatency = prometheus.NewHistogramVec(
	prometheus.HistogramOpts{
		Name: "ids_controller_reconcile_latency_seconds",
		Help: "T_ctrl: wall-time from Reconcile() entry to return, including PATCH /threshold round-trip when applicable.",
		Buckets: []float64{
			0.001, 0.005, 0.010, 0.025, 0.050, 0.100,
			0.200, 0.500, 1.000, 2.000, 5.000,
		},
	},
	[]string{"namespace", "outcome"},
)

var reconcileTotal = prometheus.NewCounterVec(
	prometheus.CounterOpts{
		Name: "ids_controller_reconcile_total",
		Help: "Total number of Reconcile() invocations, partitioned by outcome.",
	},
	[]string{"namespace", "outcome"},
)

var patchErrors = prometheus.NewCounterVec(
	prometheus.CounterOpts{
		Name: "ids_controller_threshold_patch_errors_total",
		Help: "Count of PATCH /threshold attempts that ended in a non-retriable error.",
	},
	[]string{"namespace", "reason"},
)

// e2eReadyLatency measures from the kubelet-stamped transition time to the end
// of Reconcile, so it includes API-server watch lag (seconds under load,
// hence the 30 s bucket).
var e2eReadyLatency = prometheus.NewHistogramVec(
	prometheus.HistogramOpts{
		Name: "ids_controller_e2e_ready_latency_seconds",
		Help: "End-to-end T_ctrl: pod status transition (kubelet timestamp) to PATCH /threshold ACK.",
		Buckets: []float64{
			0.01, 0.05, 0.1, 0.2, 0.5, 1.0,
			2.0, 5.0, 10.0, 30.0,
		},
	},
	[]string{"namespace", "transition", "outcome"},
)

var e2eReadyLatencyUnobservable = prometheus.NewCounterVec(
	prometheus.CounterOpts{
		Name: "ids_controller_e2e_ready_latency_unobservable_total",
		Help: "Reconciles where the kubelet transition timestamp was unavailable for e2e latency.",
	},
	[]string{"namespace", "reason"},
)

func init() {
	metrics.Registry.MustRegister(
		reconcileLatency,
		reconcileTotal,
		patchErrors,
		e2eReadyLatency,
		e2eReadyLatencyUnobservable,
	)
}
