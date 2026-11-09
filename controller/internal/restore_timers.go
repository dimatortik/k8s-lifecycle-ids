package internal

import (
	"context"
	"time"

	"sigs.k8s.io/controller-runtime/pkg/log"
)

// scheduleRestore sends the clear PATCH for uid after WarmupTTL, replacing any
// timer already pending for the same pod.
func (r *PodReconciler) scheduleRestore(uid string) {
	r.timersMu.Lock()
	defer r.timersMu.Unlock()
	if r.restoreTimers == nil {
		r.restoreTimers = make(map[string]*time.Timer)
	}
	if t, ok := r.restoreTimers[uid]; ok {
		t.Stop()
	}
	// Timer.Stop cannot stop a callback that has already started, so the
	// callback checks that it is still the current timer for uid. Otherwise
	// an outdated clear could undo a newer raise.
	var myTimer *time.Timer
	myTimer = time.AfterFunc(r.WarmupTTL, func() {
		r.timersMu.Lock()
		if r.restoreTimers[uid] != myTimer {
			r.timersMu.Unlock()
			return
		}
		delete(r.restoreTimers, uid)
		r.timersMu.Unlock()
		patchCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		if err := r.clearPodThreshold(patchCtx, uid); err != nil {
			log.Log.Info("deferred restore: clear PATCH returned non-success (likely benign)",
				"pod_uid", uid, "error", err.Error())
		}
	})
	r.restoreTimers[uid] = myTimer
}

// cancelRestoreTimer stops and forgets the pending deferred clear for uid, if any.
func (r *PodReconciler) cancelRestoreTimer(uid string) {
	r.timersMu.Lock()
	defer r.timersMu.Unlock()
	if t, ok := r.restoreTimers[uid]; ok {
		t.Stop()
		delete(r.restoreTimers, uid)
	}
}
