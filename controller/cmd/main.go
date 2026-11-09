package main

import (
	"flag"
	"net/http"
	"os"
	"time"

	"k8s.io/apimachinery/pkg/labels"
	"k8s.io/apimachinery/pkg/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/log/zap"
	metricsserver "sigs.k8s.io/controller-runtime/pkg/metrics/server"

	"github.com/dimatortik/k8s-lifecycle-ids/controller/internal"
)

var scheme = runtime.NewScheme()

func init() {
	_ = clientgoscheme.AddToScheme(scheme)
}

func getEnv(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

func main() {
	var (
		mlURL         = flag.String("ml-service-url", getEnv("ML_SERVICE_URL", "http://localhost:8080"), "ML service base URL")
		podSelector   = flag.String("pod-selector", getEnv("POD_SELECTOR", "ids-research.io/protected=true"), "label selector identifying protected pods")
		highThreshold = flag.Float64("high-threshold", -0.95, "θ_high used during pod warmup")
		warmupTTL     = flag.Duration("warmup-ttl", 60*time.Second, "TTL applied to per-pod threshold raises")
	)
	flag.Parse()

	ctrl.SetLogger(zap.New(zap.UseDevMode(true)))

	sel, err := labels.Parse(*podSelector)
	if err != nil {
		ctrl.Log.Error(err, "invalid pod selector", "value", *podSelector)
		os.Exit(2)
	}

	ctrl.Log.Info("pod-scoped controller starting",
		"ml_service_url", *mlURL,
		"pod_selector", sel.String(),
		"high_threshold", *highThreshold,
		"warmup_ttl", warmupTTL.String(),
	)

	mgr, err := ctrl.NewManager(ctrl.GetConfigOrDie(), ctrl.Options{
		Scheme: scheme,
		// :9090, so it does not clash with the ML service on :8080.
		Metrics: metricsserver.Options{BindAddress: ":9090"},
	})
	if err != nil {
		ctrl.Log.Error(err, "unable to start manager")
		os.Exit(1)
	}

	if err := (&internal.PodReconciler{
		Client:        mgr.GetClient(),
		MLServiceURL:  *mlURL,
		HTTPClient:    &http.Client{Timeout: 5 * time.Second},
		PodSelector:   sel,
		HighThreshold: *highThreshold,
		WarmupTTL:     *warmupTTL,
		Recorder:      mgr.GetEventRecorderFor("ids-controller"),
	}).SetupWithManager(mgr); err != nil {
		ctrl.Log.Error(err, "unable to set up PodReconciler")
		os.Exit(1)
	}

	ctrl.Log.Info("starting manager")
	if err := mgr.Start(ctrl.SetupSignalHandler()); err != nil {
		ctrl.Log.Error(err, "problem running manager")
		os.Exit(1)
	}
}
