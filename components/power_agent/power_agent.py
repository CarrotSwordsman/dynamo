#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Power Agent DaemonSet — Phase 1 implementation.

Runs as a privileged DaemonSet (hostPID: true) on each GPU node. Every 15s:
  1. Lists all pods on this node via the K8s API.
  2. For each physical GPU: nvmlDeviceGetComputeRunningProcesses() → PID list.
  3. For each PID: reads /proc/{pid}/cgroup → extracts pod UID.
  4. Looks up the pod's dynamo.nvidia.com/gpu-power-limit annotation.
  5. Calls nvmlDeviceSetPowerManagementLimit(handle, watts × 1000).

SIGTERM handler: restores default TDP on all managed GPUs before shutdown.
Cold-start orphan recovery: UUID-gated (persisted to /var/lib/dynamo-power-agent/).
"""

import argparse
import json
import logging
import os
import re
import signal
import threading
from typing import Callable, Optional

from actuator import Actuator, DcgmActuator, NvmlActuator

# Kubernetes and NVML — imported lazily with clear error messages
try:
    import pynvml
except ImportError:
    pynvml = None  # type: ignore

try:
    from kubernetes import client as k8s_client
    from kubernetes import config as k8s_config
    from kubernetes.config.config_exception import ConfigException
except ImportError:
    k8s_client = None  # type: ignore
    k8s_config = None  # type: ignore
    ConfigException = Exception  # type: ignore

try:
    from prometheus_client import Counter, Gauge, start_http_server

    _PROMETHEUS_AVAILABLE = True
except ImportError:
    _PROMETHEUS_AVAILABLE = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("power_agent")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

POWER_ANNOTATION_KEY = "dynamo.nvidia.com/gpu-power-limit"
RECONCILE_INTERVAL_S = 15
_MANAGED_STATE_PATH = "/var/lib/dynamo-power-agent/managed_gpus.json"

# ---------------------------------------------------------------------------
# cgroup pod-UID extraction
# Handles cgroup v1 (multi-line) and v2 (single-line), systemd / cgroupfs
# drivers, Guaranteed / Burstable / BestEffort QoS, cri-containerd / cri-o.
# ---------------------------------------------------------------------------

_SYSTEMD_RE = re.compile(
    r"kubepods-(?:burstable-|besteffort-)?pod([a-fA-F0-9_]+)\.slice"
)
_CGROUPFS_RE = re.compile(
    r"/kubepods(?:/burstable|/besteffort)?/pod([a-fA-F0-9-]+)(?:/|$)"
)


def _extract_pod_uid_from_cgroup(pid: int) -> Optional[str]:
    """Recover the pod UID from /proc/{pid}/cgroup.

    Iterates lines because cgroup v1 has one line per controller hierarchy
    while cgroup v2 has a single unified line. Uses .search() so wrapper
    segments (cri-containerd, cri-o, dockershim) don't defeat the match.
    Returns None for non-K8s processes — callers skip them silently.
    """
    try:
        with open(f"/proc/{pid}/cgroup") as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    for line in lines:
        m = _SYSTEMD_RE.search(line)
        if m:
            # systemd encodes dashes as underscores in the pod-UID segment
            return m.group(1).replace("_", "-")
        m = _CGROUPFS_RE.search(line)
        if m:
            return m.group(1)
    return None  # non-K8s process — skip


# ---------------------------------------------------------------------------
# Persistent managed-GPU state (UUID-gated orphan recovery)
# ---------------------------------------------------------------------------

_previously_managed: set[str] = set()


def _load_previously_managed_gpus() -> set[str]:
    """Load the persisted set of UUIDs this agent previously capped.

    Defensive parsing — corrupt / malformed state files must never crash
    the agent's startup. Per PR #9682 CodeRabbit review, this catches a
    superset of the original (FileNotFoundError, JSONDecodeError) cases:

      * OSError (PermissionError, IsADirectoryError, NotADirectoryError,
        I/O errors) — disk problems on the host volume should NOT brick
        the agent. Returning empty means we lose the orphan-recovery
        opportunity for this restart, which is strictly better than
        CrashLoopBackOff with no caps actuated.
      * Non-dict JSON root — a file containing a top-level list / int /
        string / null would have crashed `.get(...)` with AttributeError.
      * Non-list `managed_uuids` — a misshapen value would have crashed
        `set(...)` (int) or silently iterated characters (string).
      * Non-string entries — bytes / ints / None inside the list could
        flow through to downstream `in _previously_managed` checks
        comparing against `str` UUIDs and silently never match. Coerce
        the type guard at the boundary instead.

    Every malformed-state branch logs at WARNING and returns ``set()``
    so the operator can spot the corruption in pod logs without losing
    cap-write availability.
    """
    try:
        with open(_MANAGED_STATE_PATH) as f:
            raw = json.load(f)
    except FileNotFoundError:
        return set()
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(
            "Failed to read managed-GPU state at %s (%s: %s); "
            "treating as empty. Orphan recovery will skip this startup.",
            _MANAGED_STATE_PATH,
            type(e).__name__,
            e,
        )
        return set()

    if not isinstance(raw, dict):
        logger.warning(
            "Managed-GPU state at %s has unexpected root type %s "
            "(expected object); treating as empty.",
            _MANAGED_STATE_PATH,
            type(raw).__name__,
        )
        return set()

    uuids = raw.get("managed_uuids", [])
    if not isinstance(uuids, list):
        logger.warning(
            "Managed-GPU state at %s has unexpected managed_uuids type "
            "%s (expected list); treating as empty.",
            _MANAGED_STATE_PATH,
            type(uuids).__name__,
        )
        return set()

    valid = {u for u in uuids if isinstance(u, str)}
    if len(valid) != len(uuids):
        logger.warning(
            "Managed-GPU state at %s contained %d non-string entries; "
            "dropping them. Kept %d valid UUID(s).",
            _MANAGED_STATE_PATH,
            len(uuids) - len(valid),
            len(valid),
        )
    return valid


def _persist_managed_gpus(uuids: set[str]) -> None:
    os.makedirs(os.path.dirname(_MANAGED_STATE_PATH), exist_ok=True)
    tmp = _MANAGED_STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"managed_uuids": sorted(uuids)}, f)
    os.replace(tmp, _MANAGED_STATE_PATH)  # atomic rename


def _nvml_uuid(handle) -> str:
    """Return the GPU UUID as ``str`` regardless of pynvml major version.

    The legacy ``pynvml`` package (NVIDIA bindings) returns ``bytes`` and
    callers ``.decode("ascii")`` themselves.  ``nvidia-ml-py`` (the
    officially supported successor and what newer pip releases install
    under the name ``pynvml``) returns ``str`` directly, and an
    unconditional ``.decode()`` raises ``AttributeError``.  Callers must
    go through this helper.
    """
    uuid = pynvml.nvmlDeviceGetUUID(handle)
    return uuid.decode("ascii") if isinstance(uuid, bytes) else uuid


def _record_managed_gpu_by_uuid(uuid: str) -> None:
    """Library-agnostic UUID persistence helper.

    Called by both actuator paths after a successful cap write. The UUID
    is the hardware-level identifier, identical whether obtained from
    NVML (`nvmlDeviceGetUUID`) or DCGM (`DCGM_FI_DEV_UUID`). Separating
    the persistence from the UUID source means DcgmActuator (PR B) can
    record state without reaching into the NVML helpers.
    """
    if uuid not in _previously_managed:
        _previously_managed.add(uuid)
        _persist_managed_gpus(_previously_managed)


def _record_managed_gpu_uuid(handle) -> None:
    """Called from _apply_cap() after every successful NVML write."""
    _record_managed_gpu_by_uuid(_nvml_uuid(handle))


# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------


class _NoopMetric:
    def labels(self, **_):
        return self

    def set(self, _):
        pass

    def inc(self, _=1):
        pass


class PowerAgentMetrics:
    def __init__(self, prometheus_port: int = 0) -> None:
        if _PROMETHEUS_AVAILABLE and prometheus_port > 0:
            self.applied_limit_watts = Gauge(
                "dynamo_power_agent_applied_limit_watts",
                "Power cap currently applied per physical GPU (watts).",
                labelnames=("gpu",),
            )
            self.multi_pod_gpu_total = Counter(
                "dynamo_power_agent_multi_pod_gpu_total",
                "Times a physical GPU had multiple pods (agree or conflict).",
                labelnames=("disposition",),
            )
            self.apply_failures_total = Counter(
                "dynamo_power_agent_apply_failures_total",
                "Times an actuator write (NVML nvmlDeviceSetPowerManagementLimit "
                "or DCGM dcgmConfigSet) raised — the cap was NOT applied to the "
                "GPU. Distinct from policy fallbacks (tracked by "
                "safe_default_applied_total) where the cap IS applied at safe-default.",
            )
            self.safe_default_applied_total = Counter(
                "dynamo_power_agent_safe_default_applied_total",
                "Times the safe-default cap was used (conflict or cold-start parse failure).",
            )
            self.cap_clamped_total = Counter(
                "dynamo_power_agent_cap_clamped_total",
                "Times a requested cap was clamped to per-SKU constraints.",
                labelnames=("direction",),
            )
            # DCGM-only. Distinct from apply_failures_total because the
            # underlying dcgmConfigSet succeeded (cap IS live on the GPU)
            # — only the optional dcgmConfigEnforce that registers it as
            # DCGM's target configuration for auto-reapply-after-reset
            # failed. Stays at 0 on actuator=nvml and on
            # actuator=dcgm + agent.dcgm.enforce=false.
            self.dcgm_enforce_failures_total = Counter(
                "dynamo_power_agent_dcgm_enforce_failures_total",
                "Times dcgmConfigEnforce failed AFTER a successful dcgmConfigSet "
                "(cap is live and tracked; auto-reapply-after-GPU-reset is not).",
            )
            try:
                start_http_server(prometheus_port)
                logger.info(
                    "Prometheus metrics server started on port %d", prometheus_port
                )
            except Exception as e:
                logger.warning("Failed to start Prometheus server: %s", e)
        else:
            noop = _NoopMetric()
            self.applied_limit_watts = noop
            self.multi_pod_gpu_total = noop
            self.apply_failures_total = noop
            self.safe_default_applied_total = noop
            self.cap_clamped_total = noop
            self.dcgm_enforce_failures_total = noop


# ---------------------------------------------------------------------------
# NVML helpers
# ---------------------------------------------------------------------------


def _clamp_to_constraints(
    handle, requested_w: int, gpu_idx: int, metrics: PowerAgentMetrics
) -> int:
    """Clamp `requested_w` to the SKU-defined NVML power-cap range."""
    try:
        min_mw, max_mw = pynvml.nvmlDeviceGetPowerManagementLimitConstraints(handle)
    except pynvml.NVMLError:
        return requested_w
    min_w, max_w = min_mw // 1000, max_mw // 1000
    if requested_w < min_w:
        logger.warning(
            "Requested cap %d W below SKU min %d W on GPU %d; clamping up.",
            requested_w,
            min_w,
            gpu_idx,
        )
        metrics.cap_clamped_total.labels(direction="min").inc()
        return min_w
    if requested_w > max_w:
        logger.warning(
            "Requested cap %d W above SKU max %d W on GPU %d; clamping down.",
            requested_w,
            max_w,
            gpu_idx,
        )
        metrics.cap_clamped_total.labels(direction="max").inc()
        return max_w
    return requested_w


_managed_gpu_indices: set[int] = set()


def _apply_cap(
    handle, gpu_idx: int, requested_w: int, metrics: PowerAgentMetrics
) -> None:
    """Apply NVML power cap. All writes go through here."""
    effective_w = _clamp_to_constraints(handle, requested_w, gpu_idx, metrics)
    try:
        pynvml.nvmlDeviceSetPowerManagementLimit(handle, effective_w * 1000)
        _managed_gpu_indices.add(gpu_idx)
        _record_managed_gpu_uuid(handle)
        metrics.applied_limit_watts.labels(gpu=str(gpu_idx)).set(effective_w)
    except pynvml.NVMLError as e:
        logger.error(
            "nvmlDeviceSetPowerManagementLimit GPU %d → %d W failed: %s",
            gpu_idx,
            effective_w,
            e,
        )
        metrics.apply_failures_total.inc()


# ---------------------------------------------------------------------------
# SIGTERM handler
# ---------------------------------------------------------------------------

_shutdown = threading.Event()

# Module-level reference to the active actuator. Populated by
# `PowerAgent.__init__` (line ~478, immediately after `self._actuator.init()`),
# read by the module-level `_handle_sigterm` because Python's `signal.signal`
# hands the handler a `(signum, frame)` tuple with no other context. v1.6
# wiring per review comment #6 (SIGTERM previously bypassed the actuator and
# went straight to `pynvml`, leaving DCGM's target-config record stale on the
# `actuator: dcgm` path).
_active_actuator: Optional[Actuator] = None


def _handle_sigterm(signum, frame):
    """Restore default TGP on managed GPUs via the active actuator, then shut down.

    Dispatches through `_active_actuator` so that:
      - On `actuator: nvml`, `NvmlActuator.restore_default` runs the same
        `nvmlDeviceSetPowerManagementLimit(default)` call the pre-v1.6 inline
        handler did. Externally observable behaviour is unchanged on the NVML
        path (`test_shutdown.py` covers this).
      - On `actuator: dcgm`, `DcgmActuator.restore_default` runs
        `dcgmConfigSet(mPowerLimit.val=default)` so the hostengine's
        "target configuration" record stays consistent with the driver-level
        cap. Pre-v1.6 the raw-NVML write would have desynced them: the driver
        cap returns to default, but DCGM still holds the old cap as target
        config, and DCGM would re-apply the *old* cap after the next GPU
        reset/reinit. With `enforce: true` that mismatch was particularly
        nasty because the auto-reapply specifically uses the (now-stale)
        target config.

    Defensive fallback: if `_active_actuator` is None (SIGTERM fires before
    `PowerAgent.__init__` finished registering), we go through raw NVML so the
    GPU isn't left at a custom cap — better to ungracefully restore via the
    wrong library than to abandon a cap'd GPU.
    """
    logger.info(
        "SIGTERM received — restoring default TGP on managed GPUs and shutting down."
    )
    actuator = _active_actuator
    for gpu_idx in list(_managed_gpu_indices):
        try:
            if actuator is not None:
                actuator.restore_default(gpu_idx)
                logger.info(
                    "Restored GPU %d to default TGP via %s actuator",
                    gpu_idx,
                    actuator.name,
                )
            else:
                # Fallback: actuator not yet registered. Should be rare —
                # only happens if SIGTERM fires during PowerAgent.__init__
                # before the `_active_actuator = self._actuator` line runs.
                handle = pynvml.nvmlDeviceGetHandleByIndex(gpu_idx)
                default_mw = pynvml.nvmlDeviceGetPowerManagementDefaultLimit(handle)
                pynvml.nvmlDeviceSetPowerManagementLimit(handle, default_mw)
                logger.warning(
                    "Restored GPU %d via raw NVML (actuator not registered "
                    "yet at SIGTERM time)",
                    gpu_idx,
                )
        except Exception as e:
            logger.exception("Failed to restore TGP on GPU %d: %s", gpu_idx, e)
    try:
        if actuator is not None:
            actuator.shutdown()
        else:
            pynvml.nvmlShutdown()
    except Exception:
        # We MUST proceed to `_shutdown.set()` so the run loop unblocks
        # and the container exits cleanly — re-raising here would leave
        # the agent hung on SIGTERM. But silently dropping the failure
        # made shutdown-time NVML/DCGM faults impossible to diagnose
        # from pod logs (PR #9682 CodeRabbit review on power_agent.py:355).
        # `logger.exception` writes the full traceback at ERROR level so
        # operators can correlate with hostengine / driver events.
        logger.exception(
            "Actuator/NVML shutdown raised; proceeding with agent exit anyway.",
        )
    _shutdown.set()


# ---------------------------------------------------------------------------
# Orphan cap restoration on startup (UUID-gated)
# ---------------------------------------------------------------------------


def _restore_orphaned_gpus_on_startup(actuator: Actuator) -> None:
    """Restore default TDP only on GPUs this agent previously capped AND that are now idle.

    Migrated from inline NVML to the actuator surface in v1.5 (Fix #5):
    on the DCGM path, orphan recovery must write through `nvidia-dcgm`
    too, not bypass it via raw NVML — otherwise the hostengine's
    target-configuration record (and its reset/reinit auto-reapply
    behaviour) drifts from the driver-level reality. Going through
    `actuator.restore_default` keeps a single write path per actuator.

    Two guards are preserved verbatim from the pre-v1.5 NVML-only
    implementation:

      1. UUID-gating — only touch GPUs whose UUID is in the persisted
         `managed_gpus.json`. Prevents stepping on caps applied by
         other workflows (different DGD, manual `nvidia-smi -pl`,
         vendor firmware defaults).
      2. `current_w < default_w` — only write when the cap is
         actually below default. Skips a redundant privileged write
         (and the audit-log entry it produces) when the previous
         shutdown left the GPU at default, or when something else
         already restored it.

    The Protocol now carries `current_w` and `default_w` methods
    expressly so the guard survives the migration; see actuator.py
    `Actuator` Protocol and design doc §6.1.
    """
    global _previously_managed
    _previously_managed = _load_previously_managed_gpus()
    for gpu_idx in range(actuator.device_count()):
        try:
            uuid = actuator.get_uuid(gpu_idx)
            if uuid not in _previously_managed:
                continue
            if actuator.list_running_pids(gpu_idx):
                continue  # workload running — let normal reconcile handle it
            current_w = actuator.current_w(gpu_idx)
            default_w = actuator.default_w(gpu_idx)
            if current_w < default_w:
                actuator.restore_default(gpu_idx)
                logger.info(
                    "Restored orphaned cap on idle GPU %d (%d W → %d W).",
                    gpu_idx,
                    current_w,
                    default_w,
                )
                _previously_managed.discard(uuid)
        except Exception as e:
            logger.warning("orphan-restore failed for GPU %d: %s", gpu_idx, e)
    _persist_managed_gpus(_previously_managed)


# ---------------------------------------------------------------------------
# Multi-pod-per-GPU policy
# ---------------------------------------------------------------------------


def _resolve_cap_for_gpu(
    gpu_idx: int,
    pod_annotations: list[tuple[str, Optional[str]]],
    safe_default_watts: int,
    metrics: PowerAgentMetrics,
) -> int:
    """Determine the NVML cap to apply for a GPU given the pod annotations on it.

    Policy:
      - 1 pod with annotation  → use that value.
      - 2+ pods, all agree      → use agreed value, WARNING (multi-pod is misconfig).
      - 2+ pods, conflict       → use safe_default_watts, ERROR.
      - No parseable annotation → use safe_default_watts, ERROR.
    Returns the cap in watts.
    """
    values = [v for _, v in pod_annotations if v is not None]
    if not values:
        logger.error(
            "GPU %d: no parseable annotation on any pod; applying safe default (%d W).",
            gpu_idx,
            safe_default_watts,
        )
        # Do NOT tick apply_failures_total here — the subsequent
        # actuator.apply_cap(gpu_idx, safe_default_watts) call WILL
        # make the cap live (at safe-default value), so a metric
        # whose contract is "cap NOT live" would mislead operators
        # whose dashboards alert on it. Policy-fallback is tracked
        # by safe_default_applied_total below.
        metrics.safe_default_applied_total.inc()
        return safe_default_watts

    unique = set(values)
    if len(pod_annotations) > 1:
        if len(unique) == 1:
            logger.warning(
                "GPU %d: %d pods all agree on cap %s W (multi-pod-per-GPU is unsupported topology).",
                gpu_idx,
                len(pod_annotations),
                values[0],
            )
            metrics.multi_pod_gpu_total.labels(disposition="agree").inc()
        else:
            logger.error(
                "GPU %d: %d pods with conflicting caps %s; applying safe default (%d W).",
                gpu_idx,
                len(pod_annotations),
                sorted(unique),
                safe_default_watts,
            )
            metrics.multi_pod_gpu_total.labels(disposition="conflict").inc()
            metrics.safe_default_applied_total.inc()
            return safe_default_watts

    try:
        return int(values[0])
    except (ValueError, TypeError):
        logger.error(
            "GPU %d: annotation value %r is not an integer; applying safe default (%d W).",
            gpu_idx,
            values[0],
            safe_default_watts,
        )
        # Same rationale as the no-parseable-annotation branch above:
        # annotation parse failure is policy-fallback (cap WILL be
        # applied at safe default), not an actuator-write failure.
        metrics.safe_default_applied_total.inc()
        return safe_default_watts


# ---------------------------------------------------------------------------
# Main reconcile loop
# ---------------------------------------------------------------------------


class PowerAgent:
    def __init__(
        self,
        safe_default_watts: int,
        node_name: Optional[str] = None,
        k8s_namespace: Optional[str] = None,
        prometheus_port: int = 0,
        actuator: Optional[Actuator] = None,
        actuator_factory: Optional[Callable[["PowerAgentMetrics"], Actuator]] = None,
    ) -> None:
        self.safe_default_watts = safe_default_watts
        self.node_name = node_name or os.environ.get("NODE_NAME", "")
        self.k8s_namespace = k8s_namespace
        self.metrics = PowerAgentMetrics(prometheus_port)

        if pynvml is None:
            raise RuntimeError("pynvml is required — install pynvml or nvidia-ml-py")
        if k8s_client is None:
            raise RuntimeError("kubernetes Python SDK is required — install kubernetes")

        # NVML init still happens here for the NVML path because
        # PR #9682's reconcile loop calls pynvml directly. The DCGM
        # path runs `pynvml.nvmlInit()` again inside `DcgmActuator.init()`
        # — `nvmlInit` is idempotent so the double call is harmless.
        pynvml.nvmlInit()

        # Bind the actuator. Resolution order: explicit instance >
        # factory(metrics) > default NvmlActuator(metrics). The factory
        # form is used by `main()`/`_make_actuator` because the
        # PowerAgentMetrics object isn't constructible until __init__
        # runs (the Prometheus server starts in its constructor).
        # Tests typically pass an explicit MagicMock actuator instance.
        if actuator is not None:
            self._actuator: Actuator = actuator
        elif actuator_factory is not None:
            self._actuator = actuator_factory(self.metrics)
        else:
            self._actuator = NvmlActuator(self.metrics)
        self._actuator.init()

        # Register the actuator for the module-level SIGTERM handler
        # (v1.6, per review comment #6). signal.signal-registered callbacks
        # receive only (signum, frame) — they need a module-level handle to
        # reach this actuator, and we set it as soon as init() succeeds so
        # the window between actuator-ready and SIGTERM-handler-ready is as
        # short as possible. Tests may overwrite this to inject a mock.
        global _active_actuator
        _active_actuator = self._actuator

        self.device_count = self._actuator.device_count()
        logger.info(
            "Actuator initialized: %s. %d GPU(s) found on this node.",
            self._actuator.name,
            self.device_count,
        )

        _restore_orphaned_gpus_on_startup(self._actuator)

        # K8s client
        try:
            k8s_config.load_incluster_config()
        except ConfigException:
            k8s_config.load_kube_config()
        self._core_v1 = k8s_client.CoreV1Api()

    def _list_pods_on_node(self) -> list:
        """List all pods scheduled on this node."""
        try:
            field_selector = (
                f"spec.nodeName={self.node_name}" if self.node_name else None
            )
            if self.k8s_namespace:
                result = self._core_v1.list_namespaced_pod(
                    namespace=self.k8s_namespace,
                    field_selector=field_selector,
                )
            else:
                result = self._core_v1.list_pod_for_all_namespaces(
                    field_selector=field_selector,
                )
            return result.items
        except Exception as e:
            logger.warning("Failed to list pods: %s", e)
            return []

    def _build_uid_to_annotation(self, pods: list) -> dict[str, Optional[str]]:
        """Map pod UID → power-limit annotation value (or None if absent/malformed)."""
        result: dict[str, Optional[str]] = {}
        for pod in pods:
            uid = pod.metadata.uid
            annotations = pod.metadata.annotations or {}
            result[uid] = annotations.get(POWER_ANNOTATION_KEY)
        return result

    def reconcile_once(self) -> None:
        """Run one reconcile cycle: list pods, map PIDs→UIDs, apply caps."""
        pods = self._list_pods_on_node()
        uid_to_annotation = self._build_uid_to_annotation(pods)

        for gpu_idx in range(self.device_count):
            try:
                self._reconcile_gpu(gpu_idx, uid_to_annotation)
            except Exception as e:
                logger.error("Reconcile failed for GPU %d: %s", gpu_idx, e)

    def _reconcile_gpu(
        self,
        gpu_idx: int,
        uid_to_annotation: dict[str, Optional[str]],
    ) -> None:
        """Apply the policy-resolved cap for one GPU via the active actuator.

        v1.6 wiring per review comment #4: routes through
        `self._actuator.list_running_pids` and `self._actuator.apply_cap`
        instead of inline `pynvml`. On `actuator: dcgm` this means the
        cap write actually flows through `nvidia-dcgm` via `dcgmConfigSet`,
        which is the entire point of selecting that actuator. Pre-v1.6
        the reconcile loop hard-coded `pynvml.nvmlDeviceGetHandleByIndex`
        + module-level `_apply_cap`, so `agent.actuator=dcgm` only changed
        cold-start orphan recovery — the steady-state cap-write path
        silently used NVML regardless.

        The PID read still happens through the actuator because
        `DcgmActuator.list_running_pids` performs the v1.5 UUID-keyed
        cross-library identity lookup (DCGM gpuId -> UUID -> NVML index)
        before calling `pynvml.nvmlDeviceGetComputeRunningProcesses`.
        Bypassing the actuator here would skip that lookup and read PIDs
        from the wrong physical GPU on any node where DCGM and NVML
        disagree on enumeration order — see actuator.py
        `_ensure_identity_map` and design doc §6.3 note 5.
        """
        pids = self._actuator.list_running_pids(gpu_idx)
        if not pids:
            return  # no K8s workload on this GPU

        # Deduplicate by pod UID before building `pod_annotations`. A
        # single pod commonly runs multiple GPU processes (one per rank
        # in a TP/PP/EP topology, helper workers, profilers, etc.); the
        # pre-fix code emitted one entry per PID and would treat a
        # one-pod / two-PID GPU as if two pods were colocated. That
        # both fired the spurious "multi-pod-per-GPU" WARNING and, when
        # the pod's annotation was missing/invalid, took the
        # conflict-resolution branch in `_resolve_cap_for_gpu` (since
        # `len(pod_annotations) > 1` was true), incorrectly applying
        # safe_default + bumping multi_pod_gpu_total. Per PR #9682
        # CodeRabbit review (power_agent.py:636).
        seen_uids: set[str] = set()
        pod_annotations: list[tuple[str, Optional[str]]] = []
        for pid in pids:
            uid = _extract_pod_uid_from_cgroup(pid)
            if uid is None:
                continue  # non-K8s process — skip
            if uid in seen_uids:
                continue  # already counted this pod via an earlier PID
            if uid in uid_to_annotation:
                seen_uids.add(uid)
                pod_annotations.append((uid, uid_to_annotation[uid]))

        if not pod_annotations:
            return  # all processes are non-K8s

        cap_w = _resolve_cap_for_gpu(
            gpu_idx, pod_annotations, self.safe_default_watts, self.metrics
        )
        self._actuator.apply_cap(gpu_idx, cap_w)

    def run(self) -> None:
        """Main reconcile loop. Blocks until SIGTERM."""
        signal.signal(signal.SIGTERM, _handle_sigterm)
        signal.signal(signal.SIGINT, _handle_sigterm)

        logger.info(
            "Power Agent started. Node=%s, safe_default=%dW, interval=%ds",
            self.node_name or "(all)",
            self.safe_default_watts,
            RECONCILE_INTERVAL_S,
        )

        while not _shutdown.is_set():
            try:
                self.reconcile_once()
            except Exception as e:
                logger.exception("Unexpected error in reconcile loop: %s", e)
            _shutdown.wait(timeout=RECONCILE_INTERVAL_S)

        logger.info("Power Agent shut down.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _make_actuator(args, metrics) -> Actuator:
    """Construct the actuator declared by `--actuator`.

    Strict binary choice — `nvml` or `dcgm`. There is no auto-detection
    and no runtime probe (see design doc §6.4 / §11 Q1). The operator
    declares the actuator at chart-install time based on whether their
    cluster runs `nvidia-dcgm`; this function honors that declaration
    without modification. argparse's `choices=` guarantees `args.actuator`
    is one of the two values below, but we re-check defensively so a
    future refactor that loosens the choices doesn't silently no-op.
    """
    if args.actuator == "nvml":
        return NvmlActuator(metrics=metrics)
    if args.actuator == "dcgm":
        return DcgmActuator(
            host=args.dcgm_host,
            port=args.dcgm_port,
            enforce=args.dcgm_enforce,
            metrics=metrics,
        )
    raise ValueError(f"Unknown actuator {args.actuator!r}; expected 'nvml' or 'dcgm'.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Dynamo Power Agent DaemonSet")
    parser.add_argument(
        "--safe-default-watts",
        type=int,
        required=True,
        help="Per-GPU fail-closed cap (watts) applied when annotation parsing fails.",
    )
    parser.add_argument(
        "--node-name",
        type=str,
        default=os.environ.get("NODE_NAME", ""),
        help="K8s node name (defaults to NODE_NAME env var).",
    )
    parser.add_argument(
        "--namespace",
        type=str,
        default=None,
        help="Restrict pod watch to this K8s namespace. Default: all namespaces.",
    )
    parser.add_argument(
        "--prometheus-port",
        type=int,
        default=int(os.environ.get("PROMETHEUS_PORT", "0")),
        help="Port for Prometheus metrics (0 = disabled).",
    )
    parser.add_argument(
        "--actuator",
        choices=["nvml", "dcgm"],
        default="nvml",
        help=(
            "Power-cap actuator. 'nvml' (default) calls "
            "nvmlDeviceSetPowerManagementLimit directly — used on clusters "
            "where the GPU Operator runs with dcgm.enabled=false (the "
            "upstream default). 'dcgm' connects to the operator-managed "
            "nvidia-dcgm hostengine via TCP and uses dcgmConfigSet — used "
            "on clusters where the operator set dcgm.enabled=true. The "
            "two are mutually exclusive: a given chart deployment uses "
            "exactly one. The chart's agent.actuator value is the single "
            "source of truth; no auto-detection."
        ),
    )
    parser.add_argument(
        "--dcgm-host",
        type=str,
        default=DcgmActuator.DEFAULT_HOST,
        help=(
            "DCGM hostengine host. Default matches the upstream GPU "
            "Operator's nvidia-dcgm Service. Only consulted when "
            "--actuator=dcgm."
        ),
    )
    parser.add_argument(
        "--dcgm-port",
        type=int,
        default=DcgmActuator.DEFAULT_PORT,
        help=(
            "DCGM hostengine port. Default matches the upstream nvidia-dcgm "
            "hostPort. Only consulted when --actuator=dcgm."
        ),
    )

    def _parse_bool_strict(x: str) -> bool:
        """Strict bool parser — `treu` (typo) errors out, doesn't silently → False.

        Pre-v1.6 used `str(x).lower() in ("true","1","yes")` which mapped any
        unknown string to False, so `--dcgm-enforce treu` silently produced
        enforce=False and the operator got no feedback on the typo. argparse
        propagates the ArgumentTypeError as a parser exit-with-error, which is
        what we want at chart-install / `kubectl describe pod` time.
        """
        s = str(x).strip().lower()
        truthy = {"true", "1", "yes", "on"}
        falsy = {"false", "0", "no", "off"}
        if s in truthy:
            return True
        if s in falsy:
            return False
        raise argparse.ArgumentTypeError(
            f"--dcgm-enforce expects one of " f"{sorted(truthy | falsy)!r}; got {x!r}"
        )

    parser.add_argument(
        "--dcgm-enforce",
        type=_parse_bool_strict,
        default=False,
        help=(
            "Call dcgmConfigEnforce after each dcgmConfigSet. Default false "
            "(set-and-forget, matches NVML's semantics). Set true to "
            "register the cap as DCGM's target configuration so the "
            "hostengine re-applies it automatically after a GPU reset or "
            "reinit (DcgmConfigManager.h:113-117). This is the only "
            "automatic re-enforcement DCGM provides; it is NOT a "
            "tick-driven loop and does NOT make the cap survive Power "
            "Agent restart (the agent's SIGTERM handler restores default "
            "on every managed GPU regardless of --dcgm-enforce). Cost: "
            "one extra DCGM RPC per agent reconcile per GPU. Recommended "
            "for sites that see frequent GPU resets (XID-driven recovery, "
            "partition rebuilds, manual nvidia-smi --gpu-reset)."
        ),
    )
    args = parser.parse_args()

    agent = PowerAgent(
        safe_default_watts=args.safe_default_watts,
        node_name=args.node_name,
        k8s_namespace=args.namespace,
        prometheus_port=args.prometheus_port,
        actuator_factory=lambda metrics: _make_actuator(args, metrics),
    )
    agent.run()


if __name__ == "__main__":
    main()
