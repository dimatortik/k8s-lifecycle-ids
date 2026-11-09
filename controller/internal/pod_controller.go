// Package internal implements the Pod reconciler of the lifecycle-aware IDS
// controller.
//
// The controller watches protected Pods and tells the ML service, per pod_uid,
// which anomaly threshold to use:
//
//	deletionTimestamp set                 → clear the per-pod threshold
//	all containers Ready                  → clear it after WarmupTTL (settling period)
//	Pending, or Running but not Ready     → raise it to θ_high (warmup)
//
// It never writes to the Kubernetes API; suppression is applied over HTTP.
package internal

import (
	"context"
	"fmt"
	"net/http"
	"sync"
	"time"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/labels"
	"k8s.io/client-go/tools/record"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/builder"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/event"
	"sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/predicate"
)

// +kubebuilder:rbac:groups=core,resources=pods,verbs=get;list;watch

// PodReconciler signals the ML service with per-pod threshold PATCHes.
type PodReconciler struct {
	client.Client
	MLServiceURL string
	HTTPClient   *http.Client

	// PodSelector limits the controller to protected pods.
	PodSelector labels.Selector

	// HighThreshold (θ_high) is applied during warmup. It must be below any
	// benign warmup score so that `score < threshold` stays false.
	HighThreshold float64

	// WarmupTTL is both the TTL of a raise and the post-Ready hold: the clear
	// is deferred by WarmupTTL after the pod becomes Ready. The server-side
	// TTL is longer (serverTTLMultiplier) so it only acts as a crash safety net.
	WarmupTTL time.Duration

	timersMu      sync.Mutex
	restoreTimers map[string]*time.Timer

	// Recorder emits public ThresholdRaised / ThresholdCleared Events. The
	// adversarial attacker in the H3 experiments times its attack on them.
	Recorder record.EventRecorder
}

func (r *PodReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	logger := log.FromContext(ctx).WithValues("pod", req.NamespacedName)

	start := time.Now()
	outcome := "noop"
	defer func() {
		elapsed := time.Since(start).Seconds()
		reconcileLatency.WithLabelValues(req.Namespace, outcome).Observe(elapsed)
		reconcileTotal.WithLabelValues(req.Namespace, outcome).Inc()
	}()

	var pod corev1.Pod
	if err := r.Get(ctx, req.NamespacedName, &pod); err != nil {
		// The pod is gone and its UID is unknown; the server-side TTL removes
		// any leftover entry, and the UID is never scored again.
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}

	if r.PodSelector != nil && !r.PodSelector.Matches(labels.Set(pod.Labels)) {
		return ctrl.Result{}, nil
	}

	uid := string(pod.UID)
	if uid == "" {
		return ctrl.Result{}, nil
	}

	// End-to-end latency: from the kubelet-stamped transition (Ready condition
	// or deletionTimestamp) to this reconcile, including watch and queue delay.
	var transitionTime time.Time
	var transitionKind string
	if pod.DeletionTimestamp != nil {
		// DeletionTimestamp is the planned deletion time (request + grace).
		transitionTime = pod.DeletionTimestamp.Time
		if pod.DeletionGracePeriodSeconds != nil {
			transitionTime = transitionTime.Add(-time.Duration(*pod.DeletionGracePeriodSeconds) * time.Second)
		}
		transitionKind = "deletion"
	} else {
		for _, c := range pod.Status.Conditions {
			if c.Type == corev1.PodReady && !c.LastTransitionTime.IsZero() {
				transitionTime = c.LastTransitionTime.Time
				transitionKind = "ready"
				break
			}
		}
	}
	defer func() {
		if transitionTime.IsZero() {
			e2eReadyLatencyUnobservable.WithLabelValues(
				req.Namespace, "no_transition_timestamp").Inc()
			return
		}
		e2e := time.Since(transitionTime).Seconds()
		if e2e < 0 {
			e2eReadyLatencyUnobservable.WithLabelValues(
				req.Namespace, "negative_skew").Inc()
			return
		}
		e2eReadyLatency.WithLabelValues(
			req.Namespace, transitionKind, outcome).Observe(e2e)
	}()

	// Order matters: termination first, then Ready, then warmup.
	switch {
	case pod.DeletionTimestamp != nil:
		logger.Info("pod terminating — clearing threshold",
			"phase", pod.Status.Phase, "pod_uid", uid)
		r.cancelRestoreTimer(uid)
		if err := r.clearPodThreshold(ctx, uid); err != nil {
			outcome = "error"
			patchErrors.WithLabelValues(req.Namespace, "clear_terminating").Inc()
			return ctrl.Result{}, fmt.Errorf("clear (terminating): %w", err)
		}
		if r.Recorder != nil {
			r.Recorder.Eventf(&pod, corev1.EventTypeNormal, "ThresholdCleared",
				"Per-pod threshold cleared on pod termination (pod_uid=%s)", uid)
		}
		outcome = "clear"
		return ctrl.Result{}, nil

	case allContainersReady(&pod):
		logger.Info("pod ready — scheduling deferred restore",
			"pod_uid", uid, "after", r.WarmupTTL.String())
		r.scheduleRestore(uid)
		outcome = "restore_scheduled"
		return ctrl.Result{}, nil

	case pod.Status.Phase == corev1.PodPending || pod.Status.Phase == corev1.PodRunning:
		logger.Info("pod warming up — raising threshold",
			"phase", pod.Status.Phase, "pod_uid", uid)
		// A pod that dropped back to not-Ready may still have a pending
		// deferred clear; cancel it so it does not undo this raise.
		r.cancelRestoreTimer(uid)
		if err := r.raisePodThreshold(ctx, uid, r.HighThreshold, r.WarmupTTL); err != nil {
			outcome = "error"
			patchErrors.WithLabelValues(req.Namespace, "raise_warmup").Inc()
			return ctrl.Result{}, fmt.Errorf("raise (warmup): %w", err)
		}
		if r.Recorder != nil {
			r.Recorder.Eventf(&pod, corev1.EventTypeNormal, "ThresholdRaised",
				"Per-pod suppression threshold raised to %.4f for warmup (pod_uid=%s ttl=%s)",
				r.HighThreshold, uid, r.WarmupTTL)
		}
		outcome = "raise"
		return ctrl.Result{}, nil

	default:
		return ctrl.Result{}, nil
	}
}

// allContainersReady reads containerStatuses directly: the ContainersReady
// condition can lag one informer update behind them.
func allContainersReady(pod *corev1.Pod) bool {
	if len(pod.Spec.Containers) == 0 {
		return false
	}
	if len(pod.Status.ContainerStatuses) != len(pod.Spec.Containers) {
		return false
	}
	for i := range pod.Status.ContainerStatuses {
		if !pod.Status.ContainerStatuses[i].Ready {
			return false
		}
	}
	return true
}

// podLifecyclePredicate passes only updates that change the state machine
// inputs: deletionTimestamp, status.phase or container readiness.
func podLifecyclePredicate() predicate.Predicate {
	return predicate.Funcs{
		CreateFunc:  func(e event.CreateEvent) bool { return true },
		DeleteFunc:  func(e event.DeleteEvent) bool { return true },
		GenericFunc: func(e event.GenericEvent) bool { return true },
		UpdateFunc: func(e event.UpdateEvent) bool {
			oldPod, okOld := e.ObjectOld.(*corev1.Pod)
			newPod, okNew := e.ObjectNew.(*corev1.Pod)
			if !okOld || !okNew {
				return true
			}
			if (oldPod.DeletionTimestamp == nil) != (newPod.DeletionTimestamp == nil) {
				return true
			}
			if oldPod.Status.Phase != newPod.Status.Phase {
				return true
			}
			return allContainersReady(oldPod) != allContainersReady(newPod)
		},
	}
}

// SetupWithManager registers the reconciler for Pods with the lifecycle predicate.
func (r *PodReconciler) SetupWithManager(mgr ctrl.Manager) error {
	b := ctrl.NewControllerManagedBy(mgr).
		Named("pod-lifecycle-ids").
		For(&corev1.Pod{}, builder.WithPredicates(podLifecyclePredicate()))
	return b.Complete(r)
}
