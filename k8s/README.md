# k8s

| Path | What |
|---|---|
| `tracee/daemonset.yaml` | Tracee 0.24.1 DaemonSet (privileged, hostPID), 4 events, metrics on :3366 |
| `ml-service/deployment.yaml` | ml-service Deployment + Service (`ids-system`, port 8080) |
| `ids-controller/deployment.yaml` | controller Deployment, RBAC (read Pods, create Events), metrics Service |
| `monitoring/` | ServiceMonitor (controller) and PodMonitor (Tracee) for kube-prometheus-stack |
| `scenarios/` | H3 attacker pods: exfiltration (wget to a sinkhole), reverse shell (socat on loopback), service-account token theft |

The image fields hold placeholders; `scripts/infra/deploy_workload.sh`
replaces them with the Artifact Registry URIs. Online Boutique itself comes
from the upstream manifest and is patched by the same script (protected
label, `maxSurge=1`, `maxUnavailable=0`).

The attacker pods only talk to `192.0.2.1` (RFC 5737 TEST-NET-1, unroutable),
to loopback, and to the in-cluster API server, which denies them under
default RBAC.
