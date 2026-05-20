# SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Actuator abstraction for the Power Agent.

Defines the `Actuator` Protocol and two implementations:

  - `NvmlActuator` — the path that shipped in PR #9682. Used on
    clusters where the GPU Operator's `dcgm.enabled=false` (default).
    Delegates to the existing module-level NVML helpers in
    `power_agent.py` so the 43 PR A tests pass unchanged.
  - `DcgmActuator` — used on clusters where the GPU Operator's
    `dcgm.enabled=true`. Connects standalone-TCP to the
    `nvidia-dcgm` hostengine and routes per-GPU `dcgmConfigSet`
    + optional `dcgmConfigEnforce` writes through it. PID
    enumeration intentionally stays on NVML because DCGM does not
    expose a snapshot-of-running-PIDs API
    (`DCGM_FI_DEV_COMPUTE_PIDS` is a time-series field; see §6.3
    note 2 of the design doc).

The two are mutually exclusive at chart-install time — a Power
Agent process binds to exactly one actuator at startup and holds
it for its lifetime. See `docs/design-docs/power-agent-dual-actuator.md`
§6.4 for the selection mechanic and §6.6 for the
mutual-exclusion-by-construction guarantees.

Lazy imports
------------
Every method that touches `pynvml`, `pydcgm`, `dcgm_structs`,
`dcgm_agent`, or `dcgm_fields` imports it inside the method body
rather than at module top. Three reasons:

  1. The NVML-only deployment (the chart default) must not pay the
     `libdcgm.so` dlopen cost (~3 s on slow base images).
  2. Tests patch `power_agent.pynvml`; the `NvmlActuator` methods
     access pynvml via that re-exported reference so the patches
     stay effective without test changes.
  3. Avoids a circular import (`power_agent` imports `actuator`,
     `NvmlActuator` calls into `power_agent`).
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional, Protocol, TypeVar, runtime_checkable

logger = logging.getLogger("power_agent.actuator")

T = TypeVar("T")


@runtime_checkable
class Actuator(Protocol):
    """Protocol implemented by every power-cap actuator.

    Two implementations exist:
      - NvmlActuator (default; PR #9682 behaviour unchanged)
      - DcgmActuator (opt-in via --actuator=dcgm; added in PR B)

    The two are mutually exclusive at chart-install time — a given
    Power Agent process binds to exactly one actuator at startup and
    holds it for its lifetime. See §6.4 / §6.6 of the design doc.
    """

    name: str  # "nvml" | "dcgm"

    def init(self) -> None:
        """Initialize the underlying library. Called once at agent startup."""
        ...

    def shutdown(self) -> None:
        """Release library resources. Called on agent shutdown."""
        ...

    def device_count(self) -> int:
        """Return the number of GPUs visible to this actuator."""
        ...

    def get_uuid(self, gpu_idx: int) -> str:
        """Return the GPU UUID string for the given index."""
        ...

    def list_running_pids(self, gpu_idx: int) -> list[int]:
        """Snapshot the compute PIDs currently running on the GPU."""
        ...

    def constraints_w(self, gpu_idx: int) -> tuple[int, int]:
        """Return the (min_w, max_w) SKU power-cap settable range."""
        ...

    def current_w(self, gpu_idx: int) -> int:
        """Return the power cap currently applied to the GPU (watts).

        Distinct from `default_w` and from `constraints_w` — this is
        whatever value the driver has live on the GPU right now, set
        by whichever process last wrote it (us, nvidia-smi, vendor
        firmware default). Used by the orphan-recovery guard to skip
        no-op writes when the cap is already at default.
        """
        ...

    def default_w(self, gpu_idx: int) -> int:
        """Return the factory-default TGP for the GPU (watts).

        NVML side: `nvmlDeviceGetPowerManagementDefaultLimit`. DCGM
        side: `DCGM_FI_DEV_POWER_MGMT_LIMIT_DEF` (field 163, distinct
        from `_MAX` field 162; see DcgmActuator.restore_default).
        """
        ...

    def apply_cap(self, gpu_idx: int, watts: int) -> int:
        """Write a per-GPU power cap. Returns the effective post-clamp value.

        Return-value contract (v1.6 clarification per review comment #9):

            The returned int is the **effective post-clamp value** —
            `max(min_w, min(watts, max_w))` against the SKU constraints —
            regardless of whether the underlying write to NVML / DCGM
            succeeded. This matches both implementations' actual behaviour
            (NvmlActuator returns effective_w even on NVMLError; DcgmActuator
            returns effective_w even on DCGMError). The reason: callers
            (`PowerAgent._reconcile_gpu`, Prometheus exporters, log
            aggregators) need a non-Optional value to record what the agent
            *intended* to apply; success/failure is reported separately via
            `metrics.apply_failures_total`. Earlier doc wording said "actually
            applied" which suggested a contract this method does not hold —
            corrected here.

        A failed write does NOT raise from `apply_cap` itself; the actuator
        logs the failure and increments `apply_failures_total`. Callers that
        need to detect failure should observe the metric, not the return
        value.
        """
        ...

    def restore_default(self, gpu_idx: int) -> None:
        """Restore the factory-default TGP for the GPU."""
        ...


class NvmlActuator:
    """NVML-backed actuator — the default and only path in PR #9682.

    All methods lazy-import `power_agent` (which owns the module-level
    NVML helpers, `_managed_gpu_indices`, and the persistent state) and
    `pynvml` to keep the test patches that target `power_agent.pynvml`
    effective. This intentionally re-uses the exact code paths the
    existing 43 tests cover.

    Construction
    ------------
    `apply_cap` and `restore_default` need a metrics object to record
    clamping events, apply failures, and the applied-limit gauge.
    `PowerAgent` constructs the actuator with its own metrics
    (`NvmlActuator(self.metrics)`); tests that only exercise queries
    (`device_count`, `get_uuid`, etc.) can construct with no args.
    """

    name: str = "nvml"

    def __init__(self, metrics: Optional[Any] = None) -> None:
        # `metrics` is `power_agent.PowerAgentMetrics` in production. Typed
        # as Any here so this module doesn't need to import power_agent at
        # module load time (would create a cycle: power_agent imports
        # actuator → actuator imports power_agent at top level → ImportError).
        self._metrics = metrics

    def init(self) -> None:
        # power_agent.PowerAgent.__init__ already calls pynvml.nvmlInit()
        # before constructing the actuator in PR A; we accept that as our
        # init contract. PR B's DcgmActuator does its own connection
        # setup here, so the Protocol still earns its keep — PR B will
        # also move NVML init into this method for symmetry.
        return None

    def shutdown(self) -> None:
        # power_agent._handle_sigterm calls pynvml.nvmlShutdown() at
        # SIGTERM time; the actuator's shutdown is a no-op in PR A to
        # avoid a double-shutdown. PR B will broaden this for DCGM and
        # consolidate the NVML side in a follow-up.
        return None

    def device_count(self) -> int:
        import pynvml

        return pynvml.nvmlDeviceGetCount()

    def get_uuid(self, gpu_idx: int) -> str:
        import power_agent
        import pynvml

        handle = pynvml.nvmlDeviceGetHandleByIndex(gpu_idx)
        return power_agent._nvml_uuid(handle)

    def list_running_pids(self, gpu_idx: int) -> list[int]:
        import pynvml

        handle = pynvml.nvmlDeviceGetHandleByIndex(gpu_idx)
        return [p.pid for p in pynvml.nvmlDeviceGetComputeRunningProcesses(handle)]

    def constraints_w(self, gpu_idx: int) -> tuple[int, int]:
        import pynvml

        handle = pynvml.nvmlDeviceGetHandleByIndex(gpu_idx)
        min_mw, max_mw = pynvml.nvmlDeviceGetPowerManagementLimitConstraints(handle)
        return min_mw // 1000, max_mw // 1000

    def current_w(self, gpu_idx: int) -> int:
        """Return the cap currently applied (mW from NVML, returned in W).

        Mirrors the inline `pynvml.nvmlDeviceGetPowerManagementLimit`
        call that was in `_restore_orphaned_gpus_on_startup` before
        the v1.5 Protocol extension lifted it onto the actuator
        surface. Lazy-imports pynvml so test patches that target
        `power_agent.pynvml` (the legacy NVML test surface) stay
        effective.
        """
        import pynvml

        handle = pynvml.nvmlDeviceGetHandleByIndex(gpu_idx)
        return pynvml.nvmlDeviceGetPowerManagementLimit(handle) // 1000

    def default_w(self, gpu_idx: int) -> int:
        """Return the factory-default TGP (mW from NVML, returned in W).

        Mirrors the inline `pynvml.nvmlDeviceGetPowerManagementDefaultLimit`
        call that lived in `_restore_orphaned_gpus_on_startup` and in
        `_handle_sigterm`. Read once, then compared against
        `current_w` by the orphan-recovery guard.
        """
        import pynvml

        handle = pynvml.nvmlDeviceGetHandleByIndex(gpu_idx)
        return pynvml.nvmlDeviceGetPowerManagementDefaultLimit(handle) // 1000

    def apply_cap(self, gpu_idx: int, watts: int) -> int:
        """Apply a power cap to GPU `gpu_idx`, returning the effective watts.

        Delegates to `power_agent._apply_cap`, which contains the
        clamping, managed-state tracking, metrics updates, and
        NVMLError handling exercised by `test_apply_cap.py`.
        `_apply_cap` returns `None` (predates the Protocol), so we
        re-derive the post-clamp effective_w from constraints here
        WITHOUT calling `_clamp_to_constraints` again — a previous
        version did, which double-logged the clamp warning and
        double-incremented `cap_clamped_total` for any out-of-range
        request (v1.6 fix per review comment #8). Constraint reads
        are pure (no side effects), so reading min/max here and
        clamping locally is equivalent to peeking at what `_apply_cap`
        will produce.
        """
        if self._metrics is None:
            raise RuntimeError(
                "NvmlActuator.apply_cap requires a metrics object; "
                "construct as NvmlActuator(power_agent_metrics)."
            )

        import power_agent
        import pynvml

        handle = pynvml.nvmlDeviceGetHandleByIndex(gpu_idx)
        # _apply_cap runs the SINGLE clamp (with its log + metric side-
        # effects). We only need the post-clamp number to return.
        power_agent._apply_cap(handle, gpu_idx, watts, self._metrics)
        try:
            min_mw, max_mw = pynvml.nvmlDeviceGetPowerManagementLimitConstraints(handle)
            min_w, max_w = min_mw // 1000, max_mw // 1000
            return max(min_w, min(watts, max_w))
        except pynvml.NVMLError:
            # If constraints read fails, fall back to the requested
            # value — _apply_cap's own _clamp_to_constraints has the
            # same fallback (`return requested_w`), so the two paths
            # remain consistent.
            return watts

    def restore_default(self, gpu_idx: int) -> None:
        """Restore the factory-default TGP via NVML.

        Mirrors the inline NVML calls in `power_agent._handle_sigterm`
        and `power_agent._restore_orphaned_gpus_on_startup` so
        `DcgmActuator.restore_default` (which reads
        `DCGM_FI_DEV_POWER_MGMT_LIMIT_DEF`) is a peer of this method,
        not a special case.
        """
        import pynvml

        handle = pynvml.nvmlDeviceGetHandleByIndex(gpu_idx)
        default_mw = pynvml.nvmlDeviceGetPowerManagementDefaultLimit(handle)
        pynvml.nvmlDeviceSetPowerManagementLimit(handle, default_mw)


class DcgmActuator:
    """DCGM-backed actuator — routes per-GPU power caps through `nvidia-dcgm`.

    Used on clusters where the GPU Operator's `dcgm.enabled=true`.
    Connects standalone-TCP to the operator-managed `nvidia-dcgm`
    hostengine, calls `dcgmConfigSet(mPowerLimit.val=W)` for the cap
    write, and optionally calls `dcgmConfigEnforce` to register the
    cap as DCGM's "target configuration" so it is automatically
    re-applied after a GPU reset or reinit
    (`DcgmConfigManager.h:113-117`, `dcgm_test_apis.h:180-183`).
    That reset/reinit auto-reapply is the single resilience property
    DCGM genuinely buys over NVML — there is **no** tick-driven
    re-enforce loop in DCGM source, and `dcgmConfigEnforce` does
    **not** make the cap survive Power Agent restart (the agent's
    SIGTERM handler restores default regardless). See the design
    doc's v1.5 changelog and §7 for the corrected accounting.

    Asymmetric reads (intentional)
    ------------------------------
    `list_running_pids` calls `pynvml.nvmlDeviceGetComputeRunningProcesses`
    even on the DCGM path. DCGM has no public snapshot-of-running-PIDs
    API: `DCGM_FI_DEV_COMPUTE_PIDS` is a time-series field where each
    value decodes to one `c_dcgmRunningProcess_t` (per
    `dcgm_field_helpers.py:67-71`), and there is no
    `dcgmGetDeviceProcesses` exported in `libdcgm.so`. This is a
    property of the upstream API shapes, not a design preference —
    DCGM is built for time-series GPU monitoring, NVML for snapshot
    device queries. See design doc §6.3 note 2 / §11 Q3.

    Stale-handle recovery
    ---------------------
    When the operator restarts `nvidia-dcgm` (pod eviction, upgrade,
    chart rollback), the cached `DcgmHandle` and per-GPU `DcgmGroup`
    objects become invalid; the next API call raises
    `dcgmExceptionClass(DCGM_ST_CONNECTION_NOT_VALID)` per the
    upstream test (`DCGM/testing/python3/tests/test_connection.py:48-87`).
    Every hostengine-touching method routes through `_with_reconnect`,
    which catches that specific error code exactly once, flushes the
    group cache, rebuilds the handle, and retries. Persistent failure
    propagates so the agent's reconcile loop logs it and the next
    reconcile tick tries again. See §6.3.1.

    `restore_default` reads field 163
    --------------------------------
    DCGM exposes four distinct power-limit fields
    (`dcgm_fields.py:232-238`): MIN, MAX, DEF, current. NVML's
    `nvmlDeviceGetPowerManagementDefaultLimit` returns the
    factory default (`DEF`), which on every shipped data-center SKU
    equals MAX but is conceptually distinct. We read DEF to match
    NVML's restore semantics byte-for-byte on hypothetical future
    SKUs where default < max. See §11 Q5.
    """

    name: str = "dcgm"

    DEFAULT_HOST = "nvidia-dcgm.gpu-operator.svc.cluster.local"
    DEFAULT_PORT = 5555

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        enforce: bool = False,
        metrics: Optional[Any] = None,
    ) -> None:
        self._host = host
        self._port = port
        self._enforce = enforce
        self._metrics = metrics

        # All hostengine state — populated in init(), cleared in
        # shutdown() and _with_reconnect's recovery path.
        self._handle: Optional[Any] = None
        self._system: Optional[Any] = None
        self._discovered_gpu_ids: list[int] = []
        self._groups: dict[int, Any] = {}  # gpu_idx -> DcgmGroup

        # Cross-library identity map — built lazily on first
        # `list_running_pids` call (see _ensure_identity_map). DCGM
        # gpuIds and NVML indices live in separate identity spaces:
        # DCGM preserves gpuId across detach/attach by UUID matching
        # (DcgmCacheManager.cpp:1230-1296), while NVML re-enumerates
        # per-process; MIG can split the surfaces further. Routing
        # the NVML PID read by `gpu_idx` would therefore read the
        # wrong GPU on any node where the two libraries disagree on
        # ordering. We translate via UUID instead — the only hardware
        # identifier both libraries agree on. Cleared on reconnect
        # because DCGM may re-enumerate after the hostengine restart.
        self._dcgm_uuid_by_idx: Optional[list[str]] = None
        self._nvml_index_by_uuid: Optional[dict[str, int]] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def init(self) -> None:
        """Open the hostengine connection. Fails loud if unreachable.

        An operator who set `agent.actuator: dcgm` in the chart values
        declared that DCGM is mandatory; silently falling back to NVML
        would mask a misconfiguration. The chart catches typos at
        template time (via `validateActuator`); this method catches
        runtime unreachability with a clear stack trace.
        """
        import dcgm_structs
        import pydcgm

        self._handle = pydcgm.DcgmHandle(
            ipAddress=f"{self._host}:{self._port}",
            opMode=dcgm_structs.DCGM_OPERATION_MODE_AUTO,
            persistAfterDisconnect=False,
            timeoutMs=5000,
        )
        self._system = self._handle.GetSystem()
        # GPU enumeration uses the hostengine's view (same as
        # dcgm-exporter, dcgmi, the customer's existing DCGM stack).
        self._discovered_gpu_ids = sorted(self._system.discovery.GetAllGpuIds())

        # NVML is also initialized so list_running_pids() can call
        # nvmlDeviceGetComputeRunningProcesses (see class docstring).
        # pynvml is already a dependency of PR #9682 — no new image
        # cost.
        import pynvml

        pynvml.nvmlInit()

        logger.info(
            "DcgmActuator connected to %s:%d (enforce=%s, %d GPUs)",
            self._host,
            self._port,
            self._enforce,
            len(self._discovered_gpu_ids),
        )

    def shutdown(self) -> None:
        """Release hostengine + NVML resources.

        Defensive against the hostengine already being gone — by
        SIGTERM time it may already be evicted, so the Shutdown call
        itself can raise `DCGM_ST_CONNECTION_NOT_VALID`.
        """
        if self._handle is not None:
            for grp in self._groups.values():
                try:
                    grp.Delete()
                except Exception:
                    pass
            self._groups.clear()
            try:
                self._handle.Shutdown()
            except Exception:
                pass
            self._handle = None
            self._system = None
        # Drop the identity-map cache so a subsequent init()/use
        # rebuilds it cleanly.
        self._dcgm_uuid_by_idx = None
        self._nvml_index_by_uuid = None
        try:
            import pynvml

            pynvml.nvmlShutdown()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Stale-handle recovery
    # ------------------------------------------------------------------

    def _with_reconnect(self, op: Callable[[], T]) -> T:
        """Run `op`; on `DCGM_ST_CONNECTION_NOT_VALID`, rebuild + retry once.

        See class docstring + design doc §6.3.1 for the reasoning behind
        single-retry semantics, dropping (not Deleting) the group cache,
        and inline (not watchdog-thread) reconnection.
        """
        import dcgm_structs

        try:
            return op()
        except dcgm_structs.DCGMError as e:
            # Only recover from CONNECTION_NOT_VALID — other DCGM errors
            # (NOT_SUPPORTED, GENERIC_ERROR, etc.) indicate a different
            # class of failure that recovery would only mask.
            if e.value != dcgm_structs.DCGM_ST_CONNECTION_NOT_VALID:
                raise

            logger.warning(
                "nvidia-dcgm connection lost (DCGM_ST_CONNECTION_NOT_VALID); "
                "rebuilding handle and flushing %d cached DcgmGroup(s).",
                len(self._groups),
            )

            # Drop the stale handle + group cache. Do NOT try to
            # `.Delete()` cached groups: their backing hostengine state
            # is already gone, and the Delete call would itself raise
            # CONNECTION_NOT_VALID.
            self._groups.clear()
            # Invalidate the cross-library identity map. DCGM may
            # re-enumerate after the hostengine restart (different
            # gpuId ordering if the operator changed visibility), so
            # we must rebuild on next `list_running_pids` call.
            self._dcgm_uuid_by_idx = None
            self._nvml_index_by_uuid = None
            try:
                if self._handle is not None:
                    self._handle.Shutdown()
            except Exception:
                pass
            self._handle = None
            self._system = None

            # Re-establish handle + GPU discovery. If init() itself
            # raises, the exception propagates: a sustained outage
            # should be visible to the reconcile loop (and operators),
            # not silently retried indefinitely.
            self.init()

            # One retry. Any further error propagates so the operator
            # sees a real outage in their logs / Prometheus
            # `apply_failures_total`.
            return op()

    # ------------------------------------------------------------------
    # Query methods
    # ------------------------------------------------------------------

    def device_count(self) -> int:
        return len(self._discovered_gpu_ids)

    def get_uuid(self, gpu_idx: int) -> str:
        def _op() -> str:
            import dcgm_agent
            import dcgm_fields

            gpu_id = self._discovered_gpu_ids[gpu_idx]
            vals = dcgm_agent.dcgmEntityGetLatestValues(
                self._handle.handle,
                dcgm_fields.DCGM_FE_GPU,
                gpu_id,
                [dcgm_fields.DCGM_FI_DEV_UUID],
            )
            raw = vals[0].value.str
            return raw.decode("ascii") if isinstance(raw, bytes) else str(raw)

        return self._with_reconnect(_op)

    def list_running_pids(self, gpu_idx: int) -> list[int]:
        """Snapshot of compute PIDs on the GPU — via NVML, even on the DCGM path.

        DCGM has no public snapshot-of-running-PIDs API. See class
        docstring and design doc §6.3 note 2 for the full reasoning.
        Lazy-imports pynvml so this method is callable from tests that
        mock the import.

        Identity mapping
        ----------------
        `gpu_idx` is the DCGM-ordered index used throughout the
        reconcile loop, NOT an NVML index. We translate via UUID
        (DCGM's gpuId -> UUID via _dcgm_uuid_by_idx, then UUID ->
        NVML index via _nvml_index_by_uuid) because the two libraries
        live in separate identity spaces — see DcgmCacheManager.cpp:
        1230-1296 and design doc §6.3 note 5.
        """
        import pynvml

        self._ensure_identity_map()
        uuid = self._dcgm_uuid_by_idx[gpu_idx]
        try:
            nvml_idx = self._nvml_index_by_uuid[uuid]
        except KeyError:
            # Should not happen — _ensure_identity_map raises loudly
            # on missing UUIDs at build time. If we get here, a GPU
            # disappeared from NVML's view between identity-map build
            # and now (driver crash, hot-unplug). Surface it instead
            # of silently mis-routing.
            raise RuntimeError(
                f"DcgmActuator.list_running_pids: GPU UUID {uuid!r} "
                f"(DCGM gpu_idx={gpu_idx}) is no longer visible to "
                "NVML. Suspect mid-reconcile device hot-unplug."
            )
        handle = pynvml.nvmlDeviceGetHandleByIndex(nvml_idx)
        return [p.pid for p in pynvml.nvmlDeviceGetComputeRunningProcesses(handle)]

    def _ensure_identity_map(self) -> None:
        """Build the DCGM-idx <-> NVML-idx UUID map if not already built.

        Lazy (cap-only paths don't pay the cost). Invalidated by
        `_with_reconnect` (DCGM may re-enumerate post-restart) and
        `shutdown`. Raises RuntimeError on cross-library UUID
        mismatch — silent mis-routing of PID reads to the wrong GPU
        is the failure mode worth failing loud on. DCGM reads use
        `_with_reconnect` so the first call survives a hostengine
        restart between agent startup and first reconcile.
        """
        if self._dcgm_uuid_by_idx is not None and self._nvml_index_by_uuid is not None:
            return

        import dcgm_agent
        import dcgm_fields
        import pynvml

        def _read_dcgm_uuids() -> list[str]:
            # Indexed by gpu_idx -> _discovered_gpu_ids[gpu_idx].
            # Safe to re-iterate on _with_reconnect retry because
            # _discovered_gpu_ids is repopulated by init() on recovery.
            uuids: list[str] = []
            for gpu_id in self._discovered_gpu_ids:
                vals = dcgm_agent.dcgmEntityGetLatestValues(
                    self._handle.handle,
                    dcgm_fields.DCGM_FE_GPU,
                    gpu_id,
                    [dcgm_fields.DCGM_FI_DEV_UUID],
                )
                raw = vals[0].value.str
                uuids.append(
                    raw.decode("ascii") if isinstance(raw, bytes) else str(raw)
                )
            return uuids

        dcgm_uuids = self._with_reconnect(_read_dcgm_uuids)

        # NVML side: enumerate this process's NVML indices -> UUIDs.
        # No DCGM dependency here — bare pynvml.
        nvml_index_by_uuid: dict[str, int] = {}
        nvml_count = pynvml.nvmlDeviceGetCount()
        for nvml_idx in range(nvml_count):
            handle = pynvml.nvmlDeviceGetHandleByIndex(nvml_idx)
            raw_uuid = pynvml.nvmlDeviceGetUUID(handle)
            uuid = raw_uuid.decode("ascii") if isinstance(raw_uuid, bytes) else raw_uuid
            nvml_index_by_uuid[uuid] = nvml_idx

        missing = [u for u in dcgm_uuids if u not in nvml_index_by_uuid]
        if missing:
            raise RuntimeError(
                f"DcgmActuator: {len(missing)} GPU UUID(s) visible to "
                f"DCGM are not visible to NVML in this process: "
                f"{missing!r}. The two libraries must agree on the GPU "
                "set before the DCGM actuator can route per-GPU PID "
                "reads. Likely causes: NVIDIA_VISIBLE_DEVICES differs "
                "between the Power Agent pod and the nvidia-dcgm pod, "
                "MIG-mode mismatch, or concurrent device hot-plug."
            )

        # Publish both halves atomically so a partial write can't be
        # observed by a concurrent reader (the reconcile loop is
        # single-threaded today, but a future caller might not be).
        self._dcgm_uuid_by_idx = dcgm_uuids
        self._nvml_index_by_uuid = nvml_index_by_uuid

        logger.info(
            "DcgmActuator identity map built: %d GPUs reconciled "
            "between DCGM and NVML via UUID.",
            len(dcgm_uuids),
        )

    def constraints_w(self, gpu_idx: int) -> tuple[int, int]:
        """Return (min_w, max_w) — the SKU's settable power-cap range.

        DCGM exposes these as separate fields (MIN=161, MAX=162);
        values are in watts (not mW) per the DCGM exporter
        conventions. Distinct from `_DEF=163` which is what
        `restore_default` reads.
        """

        def _op() -> tuple[int, int]:
            import dcgm_agent
            import dcgm_fields

            gpu_id = self._discovered_gpu_ids[gpu_idx]
            vals = dcgm_agent.dcgmEntityGetLatestValues(
                self._handle.handle,
                dcgm_fields.DCGM_FE_GPU,
                gpu_id,
                [
                    dcgm_fields.DCGM_FI_DEV_POWER_MGMT_LIMIT_MIN,
                    dcgm_fields.DCGM_FI_DEV_POWER_MGMT_LIMIT_MAX,
                ],
            )
            return int(vals[0].value.dbl), int(vals[1].value.dbl)

        return self._with_reconnect(_op)

    def current_w(self, gpu_idx: int) -> int:
        """Read field 160 (DCGM_FI_DEV_POWER_MGMT_LIMIT) — current cap in W.

        Values on this field are reported in watts per the DCGM-
        exporter conventions. Distinct from `_DEF=163` (factory
        default) and `_MIN/_MAX=161/162` (settable range). Used by
        the orphan-recovery guard to skip writes that would no-op.
        """

        def _op() -> int:
            import dcgm_agent
            import dcgm_fields

            gpu_id = self._discovered_gpu_ids[gpu_idx]
            vals = dcgm_agent.dcgmEntityGetLatestValues(
                self._handle.handle,
                dcgm_fields.DCGM_FE_GPU,
                gpu_id,
                [dcgm_fields.DCGM_FI_DEV_POWER_MGMT_LIMIT],
            )
            return int(vals[0].value.dbl)

        return self._with_reconnect(_op)

    def default_w(self, gpu_idx: int) -> int:
        """Read field 163 (DCGM_FI_DEV_POWER_MGMT_LIMIT_DEF) — factory default in W.

        The byte-for-byte DCGM equivalent of NVML's
        `nvmlDeviceGetPowerManagementDefaultLimit`. Distinct from
        `_MAX=162` (max settable); on every shipped data-center SKU
        the two are numerically equal, but reading the right field
        keeps the DCGM path semantically aligned with NVML on
        hypothetical future SKUs where default < max. See §11 Q5 +
        v1.4 changelog for the discussion.
        """

        def _op() -> int:
            import dcgm_agent
            import dcgm_fields

            gpu_id = self._discovered_gpu_ids[gpu_idx]
            vals = dcgm_agent.dcgmEntityGetLatestValues(
                self._handle.handle,
                dcgm_fields.DCGM_FE_GPU,
                gpu_id,
                [dcgm_fields.DCGM_FI_DEV_POWER_MGMT_LIMIT_DEF],
            )
            return int(vals[0].value.dbl)

        return self._with_reconnect(_op)

    # ------------------------------------------------------------------
    # Write methods
    # ------------------------------------------------------------------

    def apply_cap(self, gpu_idx: int, watts: int) -> int:
        """Write a per-GPU cap via `dcgmConfigSet`; return the effective watts.

        Mirrors `power_agent._apply_cap`'s clamp + metrics + state-track
        contract on the DCGM side. DCGMError other than
        CONNECTION_NOT_VALID is absorbed into `apply_failures_total`
        and the call returns the effective post-clamp watts regardless
        (Protocol §6.1). SIGTERM / orphan-recovery callers want write
        failures surfaced as exceptions instead — they call
        `restore_default`, which uses `_apply_cap_inner`.
        """
        if self._metrics is None:
            raise RuntimeError(
                "DcgmActuator.apply_cap requires a metrics object; "
                "construct as DcgmActuator(..., metrics=power_agent_metrics)."
            )

        # Clamp against the SKU range. constraints_w handles its own
        # stale-handle recovery, so we don't re-wrap it here.
        min_w, max_w = self.constraints_w(gpu_idx)
        effective_w = self._clamp_with_metrics(watts, min_w, max_w, gpu_idx)

        try:
            return self._apply_cap_inner(gpu_idx, effective_w)
        except Exception as e:
            logger.error(
                "dcgmConfigSet GPU %d → %d W failed: %s",
                gpu_idx,
                effective_w,
                e,
            )
            self._metrics.apply_failures_total.inc()
            return effective_w

    def _apply_cap_inner(self, gpu_idx: int, effective_w: int) -> int:
        """Inner cap-write path that propagates Set failures as exceptions.

        Precondition: `effective_w` is already clamped to SKU range.
        Bookkeeping (`_managed_gpu_indices`, UUID persistence,
        `applied_limit_watts`) runs immediately after a successful
        Set — even if the optional Enforce that follows fails. The
        cap is LIVE on the GPU as soon as Set returns (DCGM invokes
        `nvmlDeviceSetPowerManagementLimit` synchronously inside
        `dcgmConfigSet`), so we MUST track it for SIGTERM and
        orphan-recovery regardless of whether the target-config
        registration via Enforce succeeded. Extracted from `apply_cap`
        so `restore_default` can surface Set failures to SIGTERM, and
        split from Enforce so partial success (Set OK, Enforce failed)
        doesn't drop managed-state tracking.
        """

        def _write_set() -> int:
            import dcgm_structs
            import pydcgm

            gpu_id = self._discovered_gpu_ids[gpu_idx]
            grp = self._groups.get(gpu_idx)
            if grp is None:
                grp = pydcgm.DcgmGroup(
                    self._handle,
                    groupName=f"dynamo-power-agent-gpu-{gpu_idx}",
                    groupType=dcgm_structs.DCGM_GROUP_EMPTY,
                )
                grp.AddGpu(gpu_id)
                self._groups[gpu_idx] = grp

            cfg = dcgm_structs.c_dcgmDeviceConfig_v2()
            cfg.version = dcgm_structs.dcgmDeviceConfig_version2
            # Blank every field DCGM would otherwise reset out from
            # under us — we only intend to write the power limit.
            cfg.mEccMode = dcgm_structs.DCGM_INT32_BLANK
            cfg.mComputeMode = dcgm_structs.DCGM_INT32_BLANK
            cfg.mPerfState.syncBoost = dcgm_structs.DCGM_INT32_BLANK
            cfg.mPerfState.targetClocks.smClock = dcgm_structs.DCGM_INT32_BLANK
            cfg.mPerfState.targetClocks.memClock = dcgm_structs.DCGM_INT32_BLANK
            # mWorkloadPowerProfiles MUST be explicitly blanked. The
            # ctypes constructor zero-initializes the array, and DCGM's
            # config manager treats an all-zero workload-profile array
            # as ACTION_CLEAR (DcgmConfigManagerTests.cpp:207-231).
            # Without this loop, every cap write would silently wipe
            # whatever workload power profiles the customer or another
            # tool had configured.
            for bitmap_index in range(
                dcgm_structs.DCGM_WORKLOAD_POWER_PROFILE_ARRAY_SIZE
            ):
                cfg.mWorkloadPowerProfiles[bitmap_index] = dcgm_structs.DCGM_INT32_BLANK
            cfg.mPowerLimit.type = dcgm_structs.DCGM_CONFIG_POWER_CAP_INDIVIDUAL
            cfg.mPowerLimit.val = effective_w

            grp.config.Set(cfg)
            return effective_w

        # Set is the load-bearing call; failure here means the cap
        # was NOT applied. Propagate so apply_cap can absorb (metric
        # tick) or restore_default can surface to SIGTERM.
        result = self._with_reconnect(_write_set)

        # Set succeeded → cap is LIVE on the GPU. Record managed state
        # before attempting Enforce so that if Enforce fails we still
        # know we're tracking this GPU.
        self._record_managed_state(gpu_idx, result)

        # Optional: register the cap as DCGM's "target configuration"
        # so the hostengine auto-reapplies after GPU reset/reinit. A
        # failure here does NOT mean the cap is gone — Set already
        # made it live. We log + tick a dedicated metric so dashboards
        # can distinguish "Enforce-only failure (cap is live)" from
        # "apply_failures_total (cap is NOT live)".
        if self._enforce:

            def _write_enforce() -> None:
                # Re-look up the group: _with_reconnect recovery would
                # have cleared self._groups, so we need to tolerate
                # absence here (the group is re-created lazily by
                # the next _write_set or this lookup raises clearly).
                grp = self._groups.get(gpu_idx)
                if grp is None:
                    raise RuntimeError(
                        f"DcgmActuator: group cache for GPU {gpu_idx} was "
                        "cleared between Set and Enforce; recovery in flight."
                    )
                grp.config.Enforce()

            try:
                self._with_reconnect(_write_enforce)
            except Exception as e:
                logger.warning(
                    "dcgmConfigEnforce failed for GPU %d after successful Set "
                    "(cap is live and tracked; only auto-reapply-after-reset "
                    "target-config registration is missing): %s",
                    gpu_idx,
                    e,
                )
                if self._metrics is not None:
                    self._metrics.dcgm_enforce_failures_total.inc()

        return result

    def _record_managed_state(self, gpu_idx: int, watts: int) -> None:
        """Record post-Set bookkeeping. Shared by Set-success path and
        the Set-OK-Enforce-failed path — both leave the cap LIVE on
        the GPU, both need SIGTERM / orphan-recovery to find it."""
        import power_agent

        power_agent._managed_gpu_indices.add(gpu_idx)
        try:
            power_agent._record_managed_gpu_by_uuid(self.get_uuid(gpu_idx))
        except Exception as e:
            # UUID lookup failure is non-fatal: the cap was applied,
            # only the persistent orphan-recovery record is missed.
            logger.warning(
                "DCGM apply succeeded on GPU %d but UUID persistence failed: %s",
                gpu_idx,
                e,
            )
        if self._metrics is not None:
            self._metrics.applied_limit_watts.labels(gpu=str(gpu_idx)).set(watts)

    def restore_default(self, gpu_idx: int) -> None:
        """Restore factory-default TGP by reading field 163 then re-capping.

        Goes through `_apply_cap_inner` (not `apply_cap`) so DCGM
        write failures propagate to the SIGTERM/orphan-recovery
        callers as exceptions instead of being absorbed. Default is
        in-constraints by definition, so skipping `apply_cap`'s
        re-clamp is safe.
        """
        self._apply_cap_inner(gpu_idx, self.default_w(gpu_idx))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _clamp_with_metrics(
        self, requested_w: int, min_w: int, max_w: int, gpu_idx: int
    ) -> int:
        """SKU clamp matching `power_agent._clamp_to_constraints` semantics.

        Re-implemented (rather than delegated) because the NVML helper
        takes a pynvml handle, which we don't have on this path. The
        metric labels and log messages are identical so dashboards see
        the same shape regardless of actuator.
        """
        if requested_w < min_w:
            logger.warning(
                "Requested cap %d W below SKU min %d W on GPU %d; clamping up.",
                requested_w,
                min_w,
                gpu_idx,
            )
            self._metrics.cap_clamped_total.labels(direction="min").inc()
            return min_w
        if requested_w > max_w:
            logger.warning(
                "Requested cap %d W above SKU max %d W on GPU %d; clamping down.",
                requested_w,
                max_w,
                gpu_idx,
            )
            self._metrics.cap_clamped_total.labels(direction="max").inc()
            return max_w
        return requested_w
