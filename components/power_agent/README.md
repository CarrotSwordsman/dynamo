# Power Agent

Privileged DaemonSet that enforces per-GPU power caps on Dynamo worker
pods. The actual cap write goes through one of two interchangeable
actuators selected at chart-install time:

- **`nvml`** (default): direct `nvmlDeviceSetPowerManagementLimit`.
  Use on clusters where the GPU Operator's `dcgm.enabled=false`
  (the upstream default).
- **`dcgm`**: writes via the operator-managed `nvidia-dcgm`
  hostengine over standalone TCP, using `dcgmConfigSet`. Use on
  clusters where the GPU Operator's `dcgm.enabled=true`.

The two paths are mutually exclusive — a given install runs exactly
one. See the [chart README](../../deploy/helm/charts/power-agent/README.md#choosing-an-actuator-nvml-or-dcgm)
for guidance on which to pick and how to install each.

## How it works

1. Every 15 seconds, the agent lists pods on the current node via the K8s API.
2. For each physical GPU, it calls `nvmlDeviceGetComputeRunningProcesses()` to get the host PIDs. (Used on **both** actuator paths — DCGM has no equivalent snapshot API; see [design doc §6.3 note 2](../../docs/design-docs/power-agent-dual-actuator.md).)
3. For each PID it reads `/proc/{pid}/cgroup` to extract the pod UID.
4. It looks up the pod's `dynamo.nvidia.com/gpu-power-limit` annotation.
5. It writes the cap via the active actuator:
   - NVML: `nvmlDeviceSetPowerManagementLimit(handle, watts × 1000)`
   - DCGM: `dcgmConfigSet(...mPowerLimit.val=watts)` plus optional
     `dcgmConfigEnforce` for re-assertion (off by default).

## Deployment

The Power Agent ships as a Helm chart at
[`deploy/helm/charts/power-agent/`](../../deploy/helm/charts/power-agent/).
Pin a release tag (the chart rejects `:latest`):

```bash
helm install power-agent ./deploy/helm/charts/power-agent \
  --namespace dynamo-system \
  --create-namespace \
  --set image.tag=v1.1.0 \
  --set agent.safeDefaultWatts=500
```

> Pin `image.tag` to match the chart's `appVersion`
> (`helm show chart deploy/helm/charts/power-agent | grep appVersion`).
> Chart v1.1.0+ renders new `--actuator` / `--dcgm-*` CLI flags and
> `power_agent.py` imports a v1.1.0-only `actuator` module — pinning
> an older image tag with a newer chart will CrashLoopBackOff at
> pod start.

See the [chart README](../../deploy/helm/charts/power-agent/README.md) for
per-SKU `safeDefaultWatts` recommendations, the in-cluster dev-iteration
mode (`dev.enabled=true`), and rollout strategy tuning for large fleets.

## Troubleshooting

Verify annotations are being set on pods:

```bash
kubectl get pods -l nvidia.com/dynamo-graph-deployment=<dgd-name> \
  -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.metadata.annotations.dynamo\.nvidia\.com/gpu-power-limit}{"\n"}{end}'
```

Check Power Agent logs:

```bash
kubectl logs -l app.kubernetes.io/name=power-agent -n <namespace> --tail=100
```

Key Prometheus metrics:
- `dynamo_power_agent_applied_limit_watts{gpu="N"}` — cap currently applied per GPU
- `dynamo_power_agent_multi_pod_gpu_total{disposition="conflict"}` — multi-pod-per-GPU conflicts (should be 0)
- `dynamo_power_agent_safe_default_applied_total` — times the safe default was used
- `dynamo_power_agent_apply_failures_total` — actuator write failed (NVML `NVMLError` or DCGM `DCGMError`); cap is NOT live on the GPU. Actuator-agnostic so dashboards work for both paths. Distinct from `safe_default_applied_total`, which counts policy fallbacks (where the cap IS live, just at safe-default value)
- `dynamo_power_agent_dcgm_enforce_failures_total` — DCGM-only: `dcgmConfigEnforce` raised AFTER a successful `dcgmConfigSet`. Cap IS live and tracked; only the auto-reapply-after-GPU-reset target-config registration failed. Stays at 0 on `actuator=nvml` and on `actuator=dcgm + agent.dcgm.enforce=false`
- `dynamo_power_agent_cap_clamped_total{direction="min|max"}` — requests clamped to SKU constraints

## Graceful shutdown

The DaemonSet sets `terminationGracePeriodSeconds: 30`. On SIGTERM, the agent
restores default TGP on all managed GPUs before exiting. If the agent is
SIGKILL'd, the GPU caps persist until the next reconcile cycle. On startup,
the agent restores default TGP on idle GPUs it previously managed (UUID-gated,
see `managed_gpus.json`).
