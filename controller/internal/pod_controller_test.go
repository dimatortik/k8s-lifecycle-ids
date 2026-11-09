package internal

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/labels"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"k8s.io/client-go/tools/record"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/event"
)

// fakeMLService records every PATCH /threshold body.
type fakeMLService struct {
	mu      sync.Mutex
	patches []thresholdPayload
	srv     *httptest.Server
}

func newFakeMLService(t *testing.T) *fakeMLService {
	f := &fakeMLService{}
	f.srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPatch || r.URL.Path != "/threshold" {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		var p thresholdPayload
		if err := json.NewDecoder(r.Body).Decode(&p); err != nil {
			t.Errorf("bad body: %v", err)
		}
		f.mu.Lock()
		f.patches = append(f.patches, p)
		f.mu.Unlock()
		w.WriteHeader(http.StatusOK)
	}))
	t.Cleanup(f.srv.Close)
	return f
}

func (f *fakeMLService) snapshot() []thresholdPayload {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]thresholdPayload(nil), f.patches...)
}

const testUID = "uid-1"

func testPod(phase corev1.PodPhase, ready bool) *corev1.Pod {
	return &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name: "cartservice-abc", Namespace: "default", UID: types.UID(testUID),
			Labels: map[string]string{"ids-research.io/protected": "true"},
		},
		Spec: corev1.PodSpec{Containers: []corev1.Container{{Name: "server"}}},
		Status: corev1.PodStatus{
			Phase:             phase,
			ContainerStatuses: []corev1.ContainerStatus{{Name: "server", Ready: ready}},
		},
	}
}

func newReconciler(t *testing.T, ml *fakeMLService, ttl time.Duration, pods ...*corev1.Pod) (*PodReconciler, *record.FakeRecorder) {
	scheme := runtime.NewScheme()
	if err := clientgoscheme.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	b := fake.NewClientBuilder().WithScheme(scheme)
	for _, p := range pods {
		b = b.WithObjects(p)
	}
	sel, _ := labels.Parse("ids-research.io/protected=true")
	rec := record.NewFakeRecorder(10)
	return &PodReconciler{
		Client:        b.Build(),
		MLServiceURL:  ml.srv.URL,
		HTTPClient:    ml.srv.Client(),
		PodSelector:   sel,
		HighThreshold: -0.95,
		WarmupTTL:     ttl,
		Recorder:      rec,
	}, rec
}

func reconcile(t *testing.T, r *PodReconciler) {
	t.Helper()
	req := ctrl.Request{NamespacedName: types.NamespacedName{Namespace: "default", Name: "cartservice-abc"}}
	if _, err := r.Reconcile(context.Background(), req); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
}

func expectEvent(t *testing.T, rec *record.FakeRecorder, reason string) {
	t.Helper()
	select {
	case e := <-rec.Events:
		if !strings.Contains(e, reason) {
			t.Fatalf("event %q does not contain %q", e, reason)
		}
	default:
		t.Fatalf("no %s event", reason)
	}
}

func TestWarmupPodRaisesThreshold(t *testing.T) {
	ml := newFakeMLService(t)
	r, rec := newReconciler(t, ml, time.Minute, testPod(corev1.PodRunning, false))
	reconcile(t, r)

	p := ml.snapshot()
	if len(p) != 1 || p[0].PodUID != testUID || p[0].Value == nil || *p[0].Value != -0.95 {
		t.Fatalf("want one raise to -0.95 for %s, got %+v", testUID, p)
	}
	if p[0].TTLSeconds == nil || *p[0].TTLSeconds != serverTTLMultiplier*60 {
		t.Fatalf("server TTL: want %d, got %v", serverTTLMultiplier*60, p[0].TTLSeconds)
	}
	expectEvent(t, rec, "ThresholdRaised")
}

func TestReadyPodClearsAfterWarmupTTL(t *testing.T) {
	ml := newFakeMLService(t)
	r, _ := newReconciler(t, ml, 50*time.Millisecond, testPod(corev1.PodRunning, true))
	reconcile(t, r)

	if n := len(ml.snapshot()); n != 0 {
		t.Fatalf("clear must be deferred, got %d PATCH(es) immediately", n)
	}
	time.Sleep(200 * time.Millisecond)
	p := ml.snapshot()
	if len(p) != 1 || p[0].Value != nil {
		t.Fatalf("want one deferred clear, got %+v", p)
	}
}

func TestNotReadyAgainCancelsPendingClear(t *testing.T) {
	ml := newFakeMLService(t)
	pod := testPod(corev1.PodRunning, true)
	r, _ := newReconciler(t, ml, 100*time.Millisecond, pod)
	reconcile(t, r) // schedules the clear

	pod.Status.ContainerStatuses[0].Ready = false
	if err := r.Status().Update(context.Background(), pod); err != nil {
		t.Fatal(err)
	}
	reconcile(t, r) // raise, cancels the timer
	time.Sleep(250 * time.Millisecond)

	p := ml.snapshot()
	if len(p) != 1 || p[0].Value == nil {
		t.Fatalf("want only the raise (no late clear), got %+v", p)
	}
}

func TestTerminatingPodClearsImmediately(t *testing.T) {
	ml := newFakeMLService(t)
	pod := testPod(corev1.PodRunning, true)
	now := metav1.Now()
	pod.DeletionTimestamp = &now
	pod.Finalizers = []string{"test"} // the fake client requires one for a deleting object
	r, rec := newReconciler(t, ml, time.Minute, pod)
	reconcile(t, r)

	p := ml.snapshot()
	if len(p) != 1 || p[0].Value != nil {
		t.Fatalf("want one immediate clear, got %+v", p)
	}
	expectEvent(t, rec, "ThresholdCleared")
}

func TestUnprotectedPodIsIgnored(t *testing.T) {
	ml := newFakeMLService(t)
	pod := testPod(corev1.PodPending, false)
	pod.Labels = map[string]string{"app": "attacker"}
	r, _ := newReconciler(t, ml, time.Minute, pod)
	reconcile(t, r)

	if n := len(ml.snapshot()); n != 0 {
		t.Fatalf("unprotected pod must not be touched, got %d PATCH(es)", n)
	}
}

func TestAllContainersReady(t *testing.T) {
	two := func(a, b bool) *corev1.Pod {
		return &corev1.Pod{
			Spec: corev1.PodSpec{Containers: []corev1.Container{{Name: "a"}, {Name: "b"}}},
			Status: corev1.PodStatus{ContainerStatuses: []corev1.ContainerStatus{
				{Name: "a", Ready: a}, {Name: "b", Ready: b}}},
		}
	}
	cases := []struct {
		name string
		pod  *corev1.Pod
		want bool
	}{
		{"all ready", two(true, true), true},
		{"one not ready", two(true, false), false},
		{"no statuses yet", &corev1.Pod{Spec: corev1.PodSpec{Containers: []corev1.Container{{Name: "a"}}}}, false},
		{"no containers", &corev1.Pod{}, false},
	}
	for _, c := range cases {
		if got := allContainersReady(c.pod); got != c.want {
			t.Errorf("%s: got %v, want %v", c.name, got, c.want)
		}
	}
}

func TestLifecyclePredicateUpdates(t *testing.T) {
	pred := podLifecyclePredicate()
	notReady := testPod(corev1.PodRunning, false)
	ready := testPod(corev1.PodRunning, true)
	relabelled := notReady.DeepCopy()
	relabelled.Labels["extra"] = "x"
	deleting := ready.DeepCopy()
	now := metav1.Now()
	deleting.DeletionTimestamp = &now

	cases := []struct {
		name     string
		old, new *corev1.Pod
		want     bool
	}{
		{"readiness change", notReady, ready, true},
		{"deletion starts", ready, deleting, true},
		{"label-only change", notReady, relabelled, false},
	}
	for _, c := range cases {
		if got := pred.Update(event.UpdateEvent{ObjectOld: c.old, ObjectNew: c.new}); got != c.want {
			t.Errorf("%s: got %v, want %v", c.name, got, c.want)
		}
	}
}
