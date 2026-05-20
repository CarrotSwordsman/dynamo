<!--
SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Power Agent Helm Chart

Privileged per-node DaemonSet that enforces per-GPU power caps on
Dynamo worker pods. Watches pods for the
`dynamo.nvidia.com/gpu-power-limit` annotation, parses
`/proc/{pid}/cgroup` to map GPU processes to pod UIDs, and writes the
cap via one of two interchangeable actuators (`nvml` or `dcgm`,
selected at chart-install time).

Supersedes the raw `deploy/power_agent/{daemonset,rbac,dev-pod}.yaml`
manifests removed in the same commit. Design rationale lives in
[`docs/design-docs/power-agent-helm-chart-plan.md`](../../../../docs/design-docs/power-agent-helm-chart-plan.md)
and (for the NVML/DCGM dual-actuator design)
[`docs/design-docs/power-agent-dual-actuator.md`](../../../../docs/design-docs/power-agent-dual-actuator.md).

## Choosing an actuator: nvml or dcgm

The chart supports two mutually exclusive paths for the actual cap
write. A given install uses exactly one — declared via `agent.actuator`
at install time, no auto-detection.

| When the GPU Operator's `dcgm.enabled` is... | Set `agent.actuator` to... | Why |
|---|---|---|
| `false` (the upstream default — `nvidia-dcgm` pod is **not** running) | `nvml` (chart default) | Direct `nvmlDeviceSetPowerManagementLimit` — exact PR #9682 behaviour, no extra dependency. |
| `true` (`nvidia-dcgm` pod **is** running and your stack relies on it for telemetry / policy) | `dcgm` | Routes writes via `dcgmConfigSet` so the Power Agent and your existing DCGM stack agree on configuration state. |

> **Operator warning — two-writer flapping.** Any secondary writer to
> NVML `nvmlDeviceSetPowerManagementLimit` *or* DCGM `dcgmConfigSet`
> will cause the cap to flap, regardless of which library either
> writer uses. The chart's mutex covers Power Agent itself, not
> third-party scripts. A `nvidia-smi -pl` cronjob on a Power Agent
> node fights with us whether we're in `nvml` or `dcgm` mode.

### Install with the default NVML actuator

```bash
helm install power-agent ./deploy/helm/charts/power-agent \
  --namespace dynamo-system \
  --create-namespace \
  --set image.tag=v1.1.0 \
  --set agent.safeDefaultWatts=500
```

> **Image tag MUST match the chart's `appVersion`** (chart v1.1.0
> requires the v1.1.0 image because the chart renders the new
> `--actuator` / `--dcgm-*` CLI flags that earlier images don't
> accept, and `power_agent.py` imports the v1.1.0-only `actuator`
> module). Confirm with `helm show chart … | grep appVersion`. Use
> `sha256:digest` for immutable production pinning.

### Install with the DCGM actuator

Prerequisite: the GPU Operator's `nvidia-dcgm` pod must be running and
its Service must be reachable from the Power Agent's namespace. If you
installed the GPU Operator with a non-default release name or
namespace, override `agent.dcgm.host` accordingly.

```bash
helm install power-agent ./deploy/helm/charts/power-agent \
  --namespace dynamo-system \
  --create-namespace \
  --set image.tag=v1.1.0 \
  --set agent.actuator=dcgm \
  --set agent.safeDefaultWatts=500
```

Optional: register the cap as DCGM's "target configuration" so the
hostengine auto-reapplies it after a GPU reset or reinit
(`DcgmConfigManager.h:113-117`):

```bash
--set agent.dcgm.enforce=true
```

Default is `false` — set-and-forget, matching NVML's behaviour. Set
`true` on sites that see frequent GPU resets (XID-driven recovery,
partition rebuilds, manual `nvidia-smi --gpu-reset`). Note: this is
the only automatic re-enforcement DCGM provides. The cap does NOT
survive Power Agent restart on either setting (the agent's SIGTERM
handler restores default regardless), and external `nvidia-smi -pl`
clobbering is repaired on the agent's next 15-s reconcile on both
the NVML and DCGM paths — there is no continuous re-enforce loop in
DCGM. See `docs/design-docs/power-agent-dual-actuator.md` §7 for the
full accounting.

## Prerequisites

- Kubernetes cluster with GPU nodes labeled `nvidia.com/gpu.present: "true"`.
- NVIDIA Container Toolkit installed on each GPU node with the `nvidia`
  runtime class registered.
- A privileged DaemonSet with `hostPID: true` is acceptable on the
  target cluster.

The chart uses the canonical NVIDIA monitoring-agent pattern:
`privileged: true` + `runtimeClassName: nvidia` +
`NVIDIA_VISIBLE_DEVICES=all`. This injects `libnvidia-ml.so` and exposes
every GPU on the node **without consuming an `nvidia.com/gpu` resource
claim** (the same pattern DCGM Exporter uses). Because the container is
privileged, the
`accept-nvidia-visible-devices-envvar-when-unprivileged=false` runtime
config does not apply.

## Production install

The chart requires an explicit image tag — `:latest` is rejected at
template time. Pin a release tag (matching the chart's `appVersion`)
or a `sha256:digest`:

```bash
helm install power-agent ./deploy/helm/charts/power-agent \
  --namespace dynamo-system \
  --create-namespace \
  --set image.tag=v1.1.0 \
  --set agent.safeDefaultWatts=500
```

Per-SKU `safeDefaultWatts` (≈70% of TDP):

| SKU         | Recommended `safeDefaultWatts` |
|-------------|-------------------------------:|
| H200 SXM    | `500` |
| H100 SXM    | `490` |
| A100 SXM 80GB | `280` |
| B200 SXM    | (consult SKU TDP) |

Verify:

```bash
kubectl rollout status daemonset/power-agent -n dynamo-system
kubectl logs -n dynamo-system -l app.kubernetes.io/name=power-agent --tail=50
kubectl get pods -n dynamo-system -l app.kubernetes.io/name=power-agent -o wide
```

## Dev install (iterating on `power_agent.py` / `actuator.py`)

Dev mode renders a single Pod pinned to one GPU node (instead of a
DaemonSet) that mounts both `power_agent.py` and `actuator.py` from
an externally-created ConfigMap. This shortens the edit-deploy-test
loop: edit the scripts locally, update the ConfigMap, restart the Pod.

> **Step 1 (REQUIRED before `helm install`).** Create the script ConfigMap.
> Both files MUST be included — `power_agent.py` imports `actuator.py`
> at module load (chart v1.1.0+), and a single-file ConfigMap will
> fail with `ModuleNotFoundError: No module named 'actuator'`.
>
> ```bash
> kubectl create configmap dynamo-power-agent-script \
>   --from-file=power_agent.py=components/power_agent/power_agent.py \
>   --from-file=actuator.py=components/power_agent/actuator.py \
>   -n $NAMESPACE
> ```
>
> Without this ConfigMap the dev Pod stays `Pending` with a
> `MountVolume.SetUp failed for volume "script"` event. The chart does
> not auto-create the ConfigMap because the iteration loop is
> `kubectl create cm --from-file=... --from-file=... --dry-run=client -o yaml | kubectl apply -f -`
> (faster than `helm upgrade --set-file`).
>
> **DCGM dev mode.** Dev pods plumb `--actuator`, `--dcgm-host`,
> `--dcgm-port`, and `--dcgm-enforce` from the same chart values the
> production DaemonSet uses (chart v1.1.0+). **Setting
> `agent.actuator=dcgm` is necessary but not sufficient** — the
> default `dev.image` is `vllm-runtime` which ships `pynvml` only.
> DCGM mode needs the vendored `pydcgm` bindings + `libdcgm.so`
> that the production `power-agent` image carries. To iterate on
> the DCGM path, override the dev image to the same one the
> production DaemonSet uses:
>
> ```bash
> --set agent.actuator=dcgm \
> --set dev.image.repository=nvcr.io/nvidia/dynamo/power-agent \
> --set dev.image.tag=v1.1.0
> ```
>
> Script iteration via the ConfigMap mount still works against this
> image — the `/scripts/power_agent.py` mount overrides the image's
> `/app/power_agent.py` because the dev-pod command line invokes
> the mount path explicitly.

Step 2: install in dev mode.

```bash
helm install power-agent ./deploy/helm/charts/power-agent \
  --namespace $NAMESPACE \
  --set image.tag=v1.1.0 \
  --set daemonset.enabled=false \
  --set dev.enabled=true \
  --set dev.nodeName=<gpu-node-name>
```

`daemonset.enabled` and `dev.enabled` are mutually exclusive — the
chart fails to render at `helm template` / `helm install` time if both
are true.

**RBAC scope in dev mode.** Dev mode automatically uses namespace-scoped
RBAC (Role + RoleBinding in the release namespace) instead of cluster-wide
RBAC, because the Pod is pinned to one node and runs the agent with
`--namespace=$POD_NAMESPACE`. This is principle-of-least-privilege by
default. If you genuinely need to test cross-namespace multi-pod conflict
resolution while iterating, override with:

```bash
--set dev.namespaceRestrictedOverride=true
```

Update flow (after editing `power_agent.py` or `actuator.py`):

```bash
kubectl create configmap dynamo-power-agent-script \
  --from-file=power_agent.py=components/power_agent/power_agent.py \
  --from-file=actuator.py=components/power_agent/actuator.py \
  -n $NAMESPACE --dry-run=client -o yaml | kubectl apply -f -

kubectl delete pod power-agent-dev -n $NAMESPACE
# Helm re-creates the Pod with the fresh ConfigMap content on next reconcile.
```

## Important values

| Key | Meaning | Default |
|-----|---------|---------|
| `image.repository` | Power Agent image registry path | `nvcr.io/nvidia/dynamo/power-agent` |
| `image.tag` | **Required** — pin to a release tag or `sha256:digest` (no default) | `""` |
| `image.pullPolicy` | Image pull policy | `IfNotPresent` |
| `imagePullSecrets` | Image pull secrets list | `[]` |
| `agent.safeDefaultWatts` | Per-SKU fail-closed cap when annotation parsing fails or multi-pod conflicts can't agree (≈70% of TDP) | `500` |
| `agent.prometheusPort` | Port exposing `/metrics` | `9100` |
| `agent.actuator` | `nvml` (default) or `dcgm`. Selects the cap-write code path. Mutually exclusive; chart-install-time choice. | `nvml` |
| `agent.dcgm.host` | Hostengine address (consulted only when `agent.actuator=dcgm`). Default matches upstream GPU Operator. | `nvidia-dcgm.gpu-operator.svc.cluster.local` |
| `agent.dcgm.port` | Hostengine port. | `5555` |
| `agent.dcgm.enforce` | When `true`, call `dcgmConfigEnforce` after each `dcgmConfigSet` so the cap is registered as DCGM's "target configuration" and auto-reapplied after GPU reset/reinit (`DcgmConfigManager.h:113-117`). Does **not** make the cap survive Power Agent restart and does **not** repair external clobbering faster than NVML; see design doc §7 corrections per v1.5. Default `false` (set-and-forget, matches NVML semantics). | `false` |
| `runtimeClassName` | NVIDIA runtime class (injects `libnvidia-ml.so`) | `nvidia` |
| `env` | Container env (`NVIDIA_VISIBLE_DEVICES`, `NVIDIA_DRIVER_CAPABILITIES`) | `{NVIDIA_VISIBLE_DEVICES: "all", NVIDIA_DRIVER_CAPABILITIES: "compute,utility"}` |
| `nodeSelector` | Target only GPU nodes | `{nvidia.com/gpu.present: "true"}` |
| `tolerations` | Tolerate the standard GPU taint | `[{key: nvidia.com/gpu, operator: Exists, effect: NoSchedule}]` |
| `resources` | Per-container CPU/mem requests + limits | small — see `values.yaml` |
| `state.hostPath` | Directory for `managed_gpus.json` UUID-gated cold-start state | `/var/lib/dynamo-power-agent` |
| `serviceAccount.create` | Create a ServiceAccount | `true` |
| `serviceAccount.name` | Override SA name (default: chart fullname) | `""` |
| `rbac.create` | Create RBAC resources | `true` |
| `rbac.namespaceRestricted` | `true` → Role+RoleBinding; `false` → ClusterRole+ClusterRoleBinding. Dev mode forces `true` regardless. | `false` |
| `daemonset.enabled` | Render production DaemonSet | `true` |
| `daemonset.updateStrategy.rollingUpdate.maxUnavailable` | Conservative `1` by default — see "Rollout strategy on large fleets" below | `1` |
| `daemonset.podLabels` / `podAnnotations` / `affinity` | Standard DS overrides | `{}` |
| `dev.enabled` | Render dev Pod instead of DaemonSet (mutually exclusive) | `false` |
| `dev.nodeName` | Required when `dev.enabled=true`; GPU node to pin to | `""` |
| `dev.scriptConfigMap` | Externally created ConfigMap holding **both** `power_agent.py` and `actuator.py` (chart v1.1.0+; a ConfigMap with only `power_agent.py` will `ModuleNotFoundError: No module named 'actuator'` at pod start) | `dynamo-power-agent-script` |
| `dev.image.repository` / `dev.image.tag` | Dev container image. Default `vllm-runtime` ships pynvml (NVML actuator works out of the box). **For DCGM dev mode**, override to the production power-agent image which vendors pydcgm + libdcgm.so: `--set dev.image.repository=nvcr.io/nvidia/dynamo/power-agent --set dev.image.tag=v1.1.0`. Script iteration via ConfigMap mount still works because `/scripts` overrides `/app` at runtime. | `nvcr.io/nvidia/ai-dynamo/vllm-runtime:1.0.1` |
| `dev.namespaceRestrictedOverride` | Set `true` to allow cluster-wide RBAC in dev mode (rare; for cross-namespace multi-pod testing only) | `false` |

See [`values.yaml`](./values.yaml) for the full configuration surface and
per-key rationale comments.

## Rollout strategy on large fleets

The default `daemonset.updateStrategy.rollingUpdate.maxUnavailable: 1`
is deliberately conservative: power-cap enforcement is safety-critical,
so only one node's worth of GPUs ever loses enforcement during a
rollout. On large fleets (hundreds of GPU nodes) where a 1-at-a-time
rollout would take too long, override:

```bash
--set daemonset.updateStrategy.rollingUpdate.maxUnavailable=10%
```

The trade-off is more concurrent enforcement gaps during the rollout
window; safe when the planner's reconcile interval (15 s) is much
shorter than the rollout duration.

## Uninstall

```bash
helm uninstall power-agent -n $NAMESPACE
```

The chart does **not** clean up `/var/lib/dynamo-power-agent/managed_gpus.json`
on host nodes. That hostPath state persists until either:

- Manual cleanup: `rm -f /var/lib/dynamo-power-agent/managed_gpus.json`
  on each affected node, **or**
- A subsequent agent install reads it during cold-start orphan recovery
  (UUID-gated; safe to leave in place across installs).

If you want a clean slate before reinstalling on a different image tag
or with different `safeDefaultWatts`, clear the file first. Otherwise
the persistent state is harmless and helps the agent restore default
TGP on GPUs it previously managed but no longer sees.

## Troubleshooting

**`MountVolume.SetUp failed for volume "script"`** in dev mode: the
script ConfigMap doesn't exist. Run the `kubectl create configmap`
recipe from the Dev install section above.

**`Error: image.tag is required (pin to a release tag or sha256:digest; :latest is not supported)`** at install time: pass `--set image.tag=<pinned>`. The chart deliberately rejects unset / `:latest` tags to keep deployments reproducible.

**`Error: daemonset.enabled and dev.enabled are mutually exclusive`** at install time: pick one mode. The chart cannot render both a DaemonSet and a single-Pod dev harness from one release.

**`Error: agent.actuator must be 'nvml' or 'dcgm'; got "..."`** at install time: typo in the actuator value (case-sensitive — `NVML` and `DCGM` are rejected). Set `--set agent.actuator=nvml` or `--set agent.actuator=dcgm`. There is no `auto` mode by design — see [dual-actuator design doc §6.4](../../../../docs/design-docs/power-agent-dual-actuator.md).

**Power Agent pod crashes at startup with `pydcgm.dcgm_structs.DCGMError: ConnectionNotValid`** after install with `agent.actuator=dcgm`: the `nvidia-dcgm` Service is unreachable. Verify (a) the GPU Operator was installed with `dcgm.enabled=true`, (b) the Service exists at the address in `agent.dcgm.host`, and (c) no NetworkPolicy is blocking egress from the Power Agent namespace to `gpu-operator/nvidia-dcgm`.

**No caps being applied at runtime**: verify the workload pods
have the `dynamo.nvidia.com/gpu-power-limit` annotation. The planner
emits it when `enable_power_awareness=true` in its config; see
[`examples/deployments/powerplanner/disagg-power-aware.yaml`](../../../../examples/deployments/powerplanner/disagg-power-aware.yaml).
