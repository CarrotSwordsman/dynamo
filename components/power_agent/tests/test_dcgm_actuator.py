# SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""PR B — DcgmActuator tests.

Asserts the DCGM path's hostengine interactions, deliberately
asymmetric reads (NVML for PIDs, DCGM for caps), and stale-handle
recovery against a fully-mocked pydcgm surface. Test scope:

  * Protocol satisfaction (DcgmActuator implements Actuator).
  * Lifecycle: init() opens the hostengine + runs nvmlInit; shutdown()
    tears down both, defensively against an already-gone hostengine.
  * Read surface: get_uuid (decodes bytes→str), constraints_w
    (reads MIN=161 + MAX=162), list_running_pids (uses NVML, NOT DCGM
    — the most load-bearing test in this file, see §6.3 note 2).
  * Write surface: apply_cap clamps the request, writes the
    `c_dcgmDeviceConfig_v2.mPowerLimit.val`, optionally calls
    Enforce(), updates module-level managed-state, and bumps
    apply_failures_total on hostengine errors.
  * restore_default reads DCGM_FI_DEV_POWER_MGMT_LIMIT_DEF (field 163,
    not MAX=162) — guards against the v1.3-era latent bug.
  * Stale-handle recovery: CONNECTION_NOT_VALID triggers single retry
    after rebuilding handle + flushing group cache. Other DCGMErrors
    propagate without retry.

Mocked at the import boundary: pydcgm/dcgm_structs/dcgm_agent/dcgm_fields
are not real Python packages in the dev/CI environment (they ship
inside the vendored DCGM container image). DcgmActuator's lazy
`import pydcgm` inside method bodies makes patch.dict('sys.modules')
clean.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import power_agent
from actuator import Actuator, DcgmActuator

# ---------------------------------------------------------------------------
# Mock factories. Centralised so individual tests stay short and the shape of
# the mocked DCGM surface is documented in one place.
# ---------------------------------------------------------------------------


def _make_dcgm_modules():
    """Build a fresh set of mocked pydcgm/dcgm_structs/dcgm_agent/dcgm_fields.

    Returns a dict suitable for `patch.dict("sys.modules", ...)`. Each
    call returns brand-new MagicMocks so cross-test contamination is
    impossible. Constants get real numeric values so any code path that
    compares them (e.g. `e.value != DCGM_ST_CONNECTION_NOT_VALID`)
    works correctly.
    """

    class DCGMError(Exception):
        """Real exception subclass — needed so try/except in production
        code catches actual instances, not MagicMock surrogates."""

        def __init__(self, value):
            super().__init__(f"DCGMError({value})")
            self.value = value

    dcgm_structs = MagicMock()
    dcgm_structs.DCGMError = DCGMError
    # Sentinel values — actual upstream constants differ but the only
    # property we rely on is that they're distinct and DCGMError.value
    # comparisons work.
    dcgm_structs.DCGM_ST_OK = 0
    dcgm_structs.DCGM_ST_CONNECTION_NOT_VALID = -42
    dcgm_structs.DCGM_ST_GENERIC_ERROR = -1
    dcgm_structs.DCGM_OPERATION_MODE_AUTO = 1
    dcgm_structs.DCGM_GROUP_EMPTY = 0
    dcgm_structs.DCGM_INT32_BLANK = 0x7FFFFFF0
    dcgm_structs.DCGM_CONFIG_POWER_CAP_INDIVIDUAL = 0
    dcgm_structs.dcgmDeviceConfig_version2 = 0x02000000
    # Matches upstream dcgm_structs.py:1475. Production code uses this
    # to bound the mWorkloadPowerProfiles blanking loop; if it's wrong
    # in the mock, the loop runs the wrong number of times and the
    # workload-profile blanking test catches that.
    dcgm_structs.DCGM_WORKLOAD_POWER_PROFILE_ARRAY_SIZE = 8

    # Each call returns a *new* config object so apply_cap's struct
    # initialization writes don't leak across test runs.
    def _new_config():
        cfg = MagicMock()
        cfg.mPerfState = MagicMock()
        cfg.mPerfState.targetClocks = MagicMock()
        cfg.mPowerLimit = MagicMock()
        return cfg

    dcgm_structs.c_dcgmDeviceConfig_v2 = MagicMock(side_effect=_new_config)

    dcgm_fields = MagicMock()
    # These constants match upstream `dcgm_fields.py` field IDs exactly
    # — load-bearing because production code passes them as field IDs
    # and the tests assert they're the *right* IDs (esp. DEF=163 vs
    # MAX=162).
    dcgm_fields.DCGM_FE_GPU = 1
    dcgm_fields.DCGM_FI_DEV_UUID = 54
    dcgm_fields.DCGM_FI_DEV_POWER_MGMT_LIMIT = 160
    dcgm_fields.DCGM_FI_DEV_POWER_MGMT_LIMIT_MIN = 161
    dcgm_fields.DCGM_FI_DEV_POWER_MGMT_LIMIT_MAX = 162
    dcgm_fields.DCGM_FI_DEV_POWER_MGMT_LIMIT_DEF = 163

    return {
        "pydcgm": MagicMock(),
        "dcgm_structs": dcgm_structs,
        "dcgm_agent": MagicMock(),
        "dcgm_fields": dcgm_fields,
    }


def _wire_handle(pydcgm_mock, gpu_ids=(0, 1)):
    """Make pydcgm.DcgmHandle(...) return a usable handle mock.

    `handle.handle` is what dcgm_agent calls receive as their first
    arg, and `handle.GetSystem().discovery.GetAllGpuIds()` is what
    `DcgmActuator.init` calls to populate `_discovered_gpu_ids`.
    """
    handle = MagicMock()
    handle.handle = 0xDEADBEEF
    handle.GetSystem.return_value.discovery.GetAllGpuIds.return_value = list(gpu_ids)
    pydcgm_mock.DcgmHandle.return_value = handle
    return handle


def _make_value(*, str_val=None, dbl_val=None):
    """Build a vals[i] mock with .value.str / .value.dbl set.

    `dcgmEntityGetLatestValues` returns a list of structs; production
    code accesses `.value.str` for UUID and `.value.dbl` for numeric
    fields. Mocking both keeps the helper reusable across read tests.
    """
    val = MagicMock()
    val.value = MagicMock()
    if str_val is not None:
        val.value.str = str_val
    if dbl_val is not None:
        val.value.dbl = dbl_val
    return val


def _make_initialized_actuator(
    modules=None, gpu_ids=(0, 1), enforce=False, metrics=None
):
    """Build + init() a DcgmActuator with a fully-wired set of mocks.

    Returns (actuator, modules, handle) so tests can assert on calls
    against any of the mocks. Saves ~10 lines of setup per test.
    """
    modules = modules or _make_dcgm_modules()
    metrics = metrics or MagicMock()
    actuator = DcgmActuator(host="test", port=5555, enforce=enforce, metrics=metrics)
    handle = _wire_handle(modules["pydcgm"], gpu_ids=gpu_ids)
    nvml = MagicMock()
    with patch.dict("sys.modules", {**modules, "pynvml": nvml}):
        actuator.init()
    return actuator, modules, handle, nvml


# ---------------------------------------------------------------------------
# Protocol satisfaction
# ---------------------------------------------------------------------------


class TestProtocolSatisfaction(unittest.TestCase):
    """DcgmActuator must structurally satisfy Actuator (@runtime_checkable)."""

    def test_dcgm_actuator_is_instance_of_actuator(self):
        # Construction must NOT touch DCGM (it's lazy until init()).
        self.assertIsInstance(DcgmActuator(), Actuator)

    def test_name_attribute_is_dcgm(self):
        self.assertEqual(DcgmActuator.name, "dcgm")
        self.assertEqual(DcgmActuator().name, "dcgm")

    def test_all_protocol_methods_present(self):
        actuator = DcgmActuator()
        for method in (
            "init",
            "shutdown",
            "device_count",
            "get_uuid",
            "list_running_pids",
            "constraints_w",
            "current_w",
            "default_w",
            "apply_cap",
            "restore_default",
        ):
            self.assertTrue(
                callable(getattr(actuator, method, None)),
                f"DcgmActuator missing or non-callable method: {method}",
            )

    def test_default_host_and_port_match_gpu_operator(self):
        """Defaults must match upstream nvidia-dcgm Service."""
        self.assertEqual(
            DcgmActuator.DEFAULT_HOST,
            "nvidia-dcgm.gpu-operator.svc.cluster.local",
        )
        self.assertEqual(DcgmActuator.DEFAULT_PORT, 5555)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestInit(unittest.TestCase):
    def test_init_opens_handle_and_runs_nvml_init(self):
        modules = _make_dcgm_modules()
        handle = _wire_handle(modules["pydcgm"], gpu_ids=(0, 1, 2, 3))
        nvml = MagicMock()
        actuator = DcgmActuator(
            host="hostengine.example", port=5555, metrics=MagicMock()
        )
        with patch.dict("sys.modules", {**modules, "pynvml": nvml}):
            actuator.init()

        modules["pydcgm"].DcgmHandle.assert_called_once()
        args, kwargs = modules["pydcgm"].DcgmHandle.call_args
        self.assertEqual(kwargs["ipAddress"], "hostengine.example:5555")
        self.assertEqual(actuator._discovered_gpu_ids, [0, 1, 2, 3])
        # Both NVML and DCGM are initialized on the DCGM path because
        # list_running_pids uses NVML even when --actuator=dcgm.
        nvml.nvmlInit.assert_called_once_with()
        # Discovery is sorted so device_count→gpu_id mapping is stable.
        handle.GetSystem.return_value.discovery.GetAllGpuIds.assert_called_once_with()

    def test_init_sorts_discovered_gpu_ids(self):
        """If DCGM returns IDs out of order, we must sort them — the
        gpu_idx → gpu_id mapping is positional and unsorted IDs would
        scramble the mapping seen by callers (annotation lookup, etc.)."""
        modules = _make_dcgm_modules()
        _wire_handle(modules["pydcgm"], gpu_ids=(3, 0, 2, 1))
        actuator = DcgmActuator(metrics=MagicMock())
        with patch.dict("sys.modules", {**modules, "pynvml": MagicMock()}):
            actuator.init()
        self.assertEqual(actuator._discovered_gpu_ids, [0, 1, 2, 3])


class TestShutdown(unittest.TestCase):
    def test_shutdown_releases_groups_handle_and_nvml(self):
        actuator, modules, handle, nvml = _make_initialized_actuator()
        # Seed a cached group so we can assert .Delete() is called.
        group_a = MagicMock()
        actuator._groups[0] = group_a

        with patch.dict("sys.modules", {**modules, "pynvml": nvml}):
            actuator.shutdown()

        group_a.Delete.assert_called_once()
        handle.Shutdown.assert_called_once()
        nvml.nvmlShutdown.assert_called_once()
        # State should be cleared so a subsequent init() rebuilds cleanly.
        self.assertEqual(actuator._groups, {})
        self.assertIsNone(actuator._handle)

    def test_shutdown_is_idempotent_against_dead_hostengine(self):
        """If the hostengine pod is already gone, shutdown() must not
        raise — SIGTERM ordering can leave us reaching for a dead
        socket. group.Delete and handle.Shutdown should be swallowed."""
        actuator, modules, handle, nvml = _make_initialized_actuator()
        group = MagicMock()
        group.Delete.side_effect = Exception("hostengine gone")
        actuator._groups[0] = group
        handle.Shutdown.side_effect = Exception("hostengine gone")

        with patch.dict("sys.modules", {**modules, "pynvml": nvml}):
            actuator.shutdown()  # must not raise

        self.assertIsNone(actuator._handle)

    def test_shutdown_logs_group_delete_failure(self):
        """Per PR #9682 CodeRabbit review on actuator.py shutdown.

        Pre-fix the group.Delete() failure was silently swallowed
        (`except Exception: pass`). We still don't re-raise (cleanup
        is best-effort) but the traceback must appear in pod logs so
        operators can diagnose DCGM resource leaks.
        """
        actuator, modules, handle, nvml = _make_initialized_actuator()
        group = MagicMock()
        group.Delete.side_effect = Exception("group delete failed")
        actuator._groups[7] = group  # specific gpu_id we'll look for in log

        with patch.dict("sys.modules", {**modules, "pynvml": nvml}):
            with self.assertLogs("power_agent.actuator", level="ERROR") as cm:
                actuator.shutdown()

        joined = "\n".join(cm.output)
        self.assertIn("group delete failed", joined)
        # The gpu_id context must be in the log message so operators
        # can spot WHICH GPU's group leaked.
        self.assertIn("gpu_id=7", joined)

    def test_shutdown_logs_hostengine_shutdown_failure(self):
        """`handle.Shutdown()` failure (e.g. DCGM_ST_CONNECTION_NOT_VALID)
        must be logged with host/port context, not silently dropped."""
        actuator, modules, handle, nvml = _make_initialized_actuator()
        handle.Shutdown.side_effect = Exception("connection not valid")

        with patch.dict("sys.modules", {**modules, "pynvml": nvml}):
            with self.assertLogs("power_agent.actuator", level="ERROR") as cm:
                actuator.shutdown()

        joined = "\n".join(cm.output)
        self.assertIn("connection not valid", joined)
        # host:port context should let operators correlate with
        # specific hostengine pod/service.
        self.assertIn(actuator._host, joined)

    def test_shutdown_logs_nvml_shutdown_failure(self):
        """`pynvml.nvmlShutdown()` failure during DcgmActuator.shutdown()
        must surface — pre-fix it was silently dropped."""
        actuator, modules, handle, nvml = _make_initialized_actuator()
        nvml.nvmlShutdown.side_effect = Exception("nvml teardown failed")

        with patch.dict("sys.modules", {**modules, "pynvml": nvml}):
            with self.assertLogs("power_agent.actuator", level="ERROR") as cm:
                actuator.shutdown()

        joined = "\n".join(cm.output)
        self.assertIn("nvml teardown failed", joined)


# ---------------------------------------------------------------------------
# Read surface
# ---------------------------------------------------------------------------


class TestDeviceCount(unittest.TestCase):
    def test_device_count_returns_len_of_discovered_ids(self):
        actuator, *_ = _make_initialized_actuator(gpu_ids=(0, 1, 2, 3))
        self.assertEqual(actuator.device_count(), 4)

    def test_device_count_zero_when_no_gpus(self):
        actuator, *_ = _make_initialized_actuator(gpu_ids=())
        self.assertEqual(actuator.device_count(), 0)


class TestGetUuid(unittest.TestCase):
    def test_get_uuid_calls_dcgm_entity_get_latest_values_with_uuid_field(self):
        actuator, modules, handle, nvml = _make_initialized_actuator()
        modules["dcgm_agent"].dcgmEntityGetLatestValues.return_value = [
            _make_value(str_val=b"GPU-deadbeef-1234"),
        ]
        with patch.dict("sys.modules", {**modules, "pynvml": nvml}):
            uuid = actuator.get_uuid(0)
        self.assertEqual(uuid, "GPU-deadbeef-1234")
        # Field ID 54 (DCGM_FI_DEV_UUID) must be in the field list.
        call = modules["dcgm_agent"].dcgmEntityGetLatestValues.call_args
        # Args: (handle, entityGroupId, entityId, fieldIds)
        self.assertEqual(call.args[3], [54])

    def test_get_uuid_handles_str_return(self):
        """Some DCGM bindings return str, others bytes. Both work."""
        actuator, modules, *_ = _make_initialized_actuator()
        modules["dcgm_agent"].dcgmEntityGetLatestValues.return_value = [
            _make_value(str_val="GPU-already-str"),
        ]
        with patch.dict("sys.modules", {**modules, "pynvml": MagicMock()}):
            self.assertEqual(actuator.get_uuid(0), "GPU-already-str")

    def test_get_uuid_uses_discovered_gpu_id_not_idx(self):
        """If DCGM enumerates GPU IDs [10, 20], gpu_idx=1 must query
        gpu_id=20, NOT gpu_id=1. Catches the off-by-mapping bug."""
        actuator, modules, *_ = _make_initialized_actuator(gpu_ids=(10, 20))
        modules["dcgm_agent"].dcgmEntityGetLatestValues.return_value = [
            _make_value(str_val=b"GPU-x"),
        ]
        with patch.dict("sys.modules", {**modules, "pynvml": MagicMock()}):
            actuator.get_uuid(1)
        call = modules["dcgm_agent"].dcgmEntityGetLatestValues.call_args
        # Args: (handle, entityGroupId, entityId, fieldIds)
        self.assertEqual(call.args[2], 20)


def _wire_identity_map(modules, nvml, dcgm_uuids, nvml_uuids):
    """Seed mocks for the lazy UUID-keyed identity map.

    `dcgm_uuids` is the list of UUIDs DCGM returns for its discovered
    GPUs, in the order `_discovered_gpu_ids` iterates them.
    `nvml_uuids` is the list of UUIDs NVML returns by index 0..N-1.
    The two lists can be in different orders (or contain different
    sets) to exercise the cross-library mapping. UUIDs may be `bytes`
    or `str`; we mirror upstream where some bindings return each.
    """
    # DCGM side: one dcgmEntityGetLatestValues call per discovered GPU,
    # each asking for [DCGM_FI_DEV_UUID].
    dcgm_responses = [[_make_value(str_val=u)] for u in dcgm_uuids]
    modules["dcgm_agent"].dcgmEntityGetLatestValues.side_effect = dcgm_responses

    # NVML side: count + per-index handle + per-handle UUID.
    nvml.nvmlDeviceGetCount.return_value = len(nvml_uuids)
    handles = [f"nvml_handle_{i}" for i in range(len(nvml_uuids))]
    nvml.nvmlDeviceGetHandleByIndex.side_effect = lambda i: handles[i]
    uuid_by_handle = dict(zip(handles, nvml_uuids))
    nvml.nvmlDeviceGetUUID.side_effect = lambda h: uuid_by_handle[h]


class TestListRunningPidsUsesNvml(unittest.TestCase):
    """The crown jewel test — DCGM path uses NVML for PIDs (§6.3 note 2).

    If a future refactor accidentally routes the PID read through DCGM
    (which has no snapshot API), the agent would lose PID enumeration
    on the DCGM path. These tests guard that contract.

    Identity-map note: as of the v1.5 fix, `list_running_pids` first
    builds (lazily) a UUID-keyed map between DCGM gpuIds and NVML
    indices, so DCGM IS called for UUID reads at first-use time. The
    "no DCGM" assertion is therefore reframed as "no DCGM PID-field
    reads" — only UUID reads (field 54) are allowed.
    """

    def test_list_running_pids_uses_pynvml_get_compute_running_processes(self):
        actuator, modules, *_ = _make_initialized_actuator()
        nvml = MagicMock()
        # Same UUIDs DCGM and NVML see, in the same order → identity map
        # is the identity function (gpu_idx 0 → nvml index 0).
        _wire_identity_map(
            modules,
            nvml,
            dcgm_uuids=[b"GPU-aaaa", b"GPU-bbbb"],
            nvml_uuids=[b"GPU-aaaa", b"GPU-bbbb"],
        )
        proc_a = MagicMock(pid=1234)
        proc_b = MagicMock(pid=5678)
        nvml.nvmlDeviceGetComputeRunningProcesses.return_value = [proc_a, proc_b]

        with patch.dict("sys.modules", {**modules, "pynvml": nvml}):
            pids = actuator.list_running_pids(0)

        self.assertEqual(pids, [1234, 5678])
        # The compute-PID enumeration itself goes through NVML.
        nvml.nvmlDeviceGetComputeRunningProcesses.assert_called_once_with(
            "nvml_handle_0"
        )
        # Every DCGM call must be for the UUID field (54) only — the
        # identity map's reads. If any other field shows up, we've
        # regressed onto the time-series PID-read path the design doc
        # warns about.
        for call in modules["dcgm_agent"].dcgmEntityGetLatestValues.call_args_list:
            self.assertEqual(
                call.args[3],
                [54],
                "DCGM was called with non-UUID field ids — "
                "list_running_pids must not use DCGM for PIDs.",
            )

    def test_list_running_pids_returns_empty_when_no_processes(self):
        actuator, modules, *_ = _make_initialized_actuator()
        nvml = MagicMock()
        _wire_identity_map(
            modules,
            nvml,
            dcgm_uuids=[b"GPU-aaaa", b"GPU-bbbb"],
            nvml_uuids=[b"GPU-aaaa", b"GPU-bbbb"],
        )
        nvml.nvmlDeviceGetComputeRunningProcesses.return_value = []
        with patch.dict("sys.modules", {**modules, "pynvml": nvml}):
            self.assertEqual(actuator.list_running_pids(0), [])

    def test_list_running_pids_routes_via_uuid_not_gpu_idx(self):
        """Misaligned DCGM/NVML orderings: same UUIDs, different index spaces.

        DCGM enumerates [10, 20] (i.e. gpu_idx 0 → DCGM gpu_id 10,
        UUID 'GPU-a'); NVML enumerates them in the opposite order
        (index 0 → 'GPU-b', index 1 → 'GPU-a'). list_running_pids(0)
        must route the NVML handle lookup by UUID, hitting NVML
        index 1, not index 0. This is the load-bearing correctness
        test for the v1.5 cross-library identity fix.
        """
        actuator, modules, *_ = _make_initialized_actuator(gpu_ids=(10, 20))
        nvml = MagicMock()
        _wire_identity_map(
            modules,
            nvml,
            dcgm_uuids=[b"GPU-a", b"GPU-b"],
            nvml_uuids=[b"GPU-b", b"GPU-a"],  # reversed!
        )
        proc = MagicMock(pid=999)
        nvml.nvmlDeviceGetComputeRunningProcesses.return_value = [proc]

        with patch.dict("sys.modules", {**modules, "pynvml": nvml}):
            pids = actuator.list_running_pids(0)

        self.assertEqual(pids, [999])
        # The handle returned for "GPU-a" lives at NVML index 1, NOT 0.
        # If routing fell through on raw gpu_idx, we'd see index 0.
        nvml.nvmlDeviceGetHandleByIndex.assert_any_call(1)
        nvml.nvmlDeviceGetComputeRunningProcesses.assert_called_once_with(
            "nvml_handle_1"
        )

    def test_identity_map_is_lazy_and_cached(self):
        """Build once, reuse on subsequent calls.

        Per-call rebuilds would spam DCGM with N UUID reads every
        reconcile (and N NVML enumerations). The map only invalidates
        on _with_reconnect; until then, both halves are reused.
        """
        actuator, modules, *_ = _make_initialized_actuator()
        nvml = MagicMock()
        _wire_identity_map(
            modules,
            nvml,
            dcgm_uuids=[b"GPU-aaaa", b"GPU-bbbb"],
            nvml_uuids=[b"GPU-aaaa", b"GPU-bbbb"],
        )
        nvml.nvmlDeviceGetComputeRunningProcesses.return_value = []

        with patch.dict("sys.modules", {**modules, "pynvml": nvml}):
            actuator.list_running_pids(0)
            actuator.list_running_pids(1)
            actuator.list_running_pids(0)

        # DCGM: one UUID read per discovered GPU, regardless of how
        # many times list_running_pids was called.
        self.assertEqual(modules["dcgm_agent"].dcgmEntityGetLatestValues.call_count, 2)
        # NVML enumeration: exactly nvml.nvmlDeviceGetCount() handle
        # lookups for the map build, plus one per list_running_pids
        # call. Count() itself called only once (during map build).
        nvml.nvmlDeviceGetCount.assert_called_once()

    def test_identity_map_raises_on_uuid_missing_from_nvml(self):
        """If DCGM sees a UUID NVML doesn't, surface it loud at first use.

        Silent mis-routing would land cap reads/writes on the wrong
        physical GPU. The operator needs to fix the visibility
        mismatch (NVIDIA_VISIBLE_DEVICES, MIG mode, etc.) — not have
        the agent quietly limp along.
        """
        actuator, modules, *_ = _make_initialized_actuator()
        nvml = MagicMock()
        _wire_identity_map(
            modules,
            nvml,
            dcgm_uuids=[b"GPU-aaaa", b"GPU-missing"],
            nvml_uuids=[b"GPU-aaaa"],
        )

        with patch.dict("sys.modules", {**modules, "pynvml": nvml}):
            with self.assertRaises(RuntimeError) as ctx:
                actuator.list_running_pids(0)

        self.assertIn("GPU-missing", str(ctx.exception))
        self.assertIn("DCGM", str(ctx.exception))
        self.assertIn("NVML", str(ctx.exception))

    def test_identity_map_invalidated_on_reconnect(self):
        """After _with_reconnect rebuilds the handle, the map must be rebuilt.

        DCGM may re-enumerate post-restart (different gpuId ordering
        if visibility changed). Stale UUID mappings would silently
        route PID reads to the wrong NVML index.
        """
        actuator, modules, *_ = _make_initialized_actuator()
        nvml = MagicMock()
        _wire_identity_map(
            modules,
            nvml,
            dcgm_uuids=[b"GPU-aaaa", b"GPU-bbbb"],
            nvml_uuids=[b"GPU-aaaa", b"GPU-bbbb"],
        )
        nvml.nvmlDeviceGetComputeRunningProcesses.return_value = []

        with patch.dict("sys.modules", {**modules, "pynvml": nvml}):
            actuator.list_running_pids(0)  # builds the map
            self.assertIsNotNone(actuator._dcgm_uuid_by_idx)

            DCGMError = modules["dcgm_structs"].DCGMError
            CONNECTION_NOT_VALID = modules["dcgm_structs"].DCGM_ST_CONNECTION_NOT_VALID

            # Trigger recovery via _with_reconnect; the second op() call
            # is irrelevant — we just want the cache clear to fire.
            calls = {"n": 0}

            def op():
                calls["n"] += 1
                if calls["n"] == 1:
                    raise DCGMError(CONNECTION_NOT_VALID)
                return None

            actuator._with_reconnect(op)

        self.assertIsNone(actuator._dcgm_uuid_by_idx)
        self.assertIsNone(actuator._nvml_index_by_uuid)

    def test_identity_map_build_recovers_from_connection_not_valid(self):
        """First list_running_pids survives a hostengine restart.

        Injects CONNECTION_NOT_VALID on the first UUID read, healthy
        responses on retry. Asserts list_running_pids returns the PIDs
        (no propagated exception), DcgmHandle was rebuilt, identity
        map is populated from the retry data.
        """
        actuator, modules, *_ = _make_initialized_actuator()
        nvml = MagicMock()

        DCGMError = modules["dcgm_structs"].DCGMError
        CONNECTION_NOT_VALID = modules["dcgm_structs"].DCGM_ST_CONNECTION_NOT_VALID

        # _with_reconnect replays the whole _read_dcgm_uuids closure on
        # retry, so post-recovery we need one UUID response per GPU.
        modules["dcgm_agent"].dcgmEntityGetLatestValues.side_effect = [
            DCGMError(CONNECTION_NOT_VALID),
            [_make_value(str_val=b"GPU-aaaa")],
            [_make_value(str_val=b"GPU-bbbb")],
        ]

        nvml.nvmlDeviceGetCount.return_value = 2
        handles = ["nvml_handle_0", "nvml_handle_1"]
        nvml.nvmlDeviceGetHandleByIndex.side_effect = lambda i: handles[i]
        nvml_uuids = {"nvml_handle_0": b"GPU-aaaa", "nvml_handle_1": b"GPU-bbbb"}
        nvml.nvmlDeviceGetUUID.side_effect = lambda h: nvml_uuids[h]
        nvml.nvmlDeviceGetComputeRunningProcesses.return_value = [MagicMock(pid=42)]

        handle_calls_before = modules["pydcgm"].DcgmHandle.call_count

        with patch.dict("sys.modules", {**modules, "pynvml": nvml}):
            pids = actuator.list_running_pids(0)

        self.assertEqual(pids, [42])
        self.assertEqual(
            modules["pydcgm"].DcgmHandle.call_count,
            handle_calls_before + 1,
            "_with_reconnect should have rebuilt DcgmHandle once.",
        )
        self.assertEqual(actuator._dcgm_uuid_by_idx, ["GPU-aaaa", "GPU-bbbb"])
        self.assertEqual(actuator._nvml_index_by_uuid, {"GPU-aaaa": 0, "GPU-bbbb": 1})


class TestConstraintsW(unittest.TestCase):
    def test_constraints_w_reads_min_and_max_fields(self):
        actuator, modules, *_ = _make_initialized_actuator()
        modules["dcgm_agent"].dcgmEntityGetLatestValues.return_value = [
            _make_value(dbl_val=100.0),
            _make_value(dbl_val=700.0),
        ]
        with patch.dict("sys.modules", {**modules, "pynvml": MagicMock()}):
            min_w, max_w = actuator.constraints_w(0)
        self.assertEqual((min_w, max_w), (100, 700))
        call = modules["dcgm_agent"].dcgmEntityGetLatestValues.call_args
        # Field IDs 161 (MIN), 162 (MAX). Order matters — production
        # code unpacks vals[0]=min, vals[1]=max.
        self.assertEqual(call.args[3], [161, 162])


class TestCurrentWAndDefaultW(unittest.TestCase):
    """v1.5 Protocol additions — DCGM side reads the dedicated power-
    limit fields (160 = current, 163 = factory default). Distinct
    fields from constraints (161/162); the orphan-recovery guard
    will dispatch the wrong values if these get crossed."""

    def test_current_w_reads_field_160(self):
        """DCGM_FI_DEV_POWER_MGMT_LIMIT = 160 (current limit). NOT 162 (MAX)."""
        actuator, modules, *_ = _make_initialized_actuator()
        modules["dcgm_agent"].dcgmEntityGetLatestValues.return_value = [
            _make_value(dbl_val=423.0),
        ]
        with patch.dict("sys.modules", {**modules, "pynvml": MagicMock()}):
            result = actuator.current_w(0)
        self.assertEqual(result, 423)
        # Field ID must be 160, not 162. (Mock constants set in
        # _make_dcgm_modules; production uses the real upstream IDs.)
        call = modules["dcgm_agent"].dcgmEntityGetLatestValues.call_args
        self.assertEqual(call.args[3], [160])

    def test_default_w_reads_field_163(self):
        """DCGM_FI_DEV_POWER_MGMT_LIMIT_DEF = 163 (factory default).
        NOT 162 (MAX) — the v1.4 latent-bug fix made this distinct."""
        actuator, modules, *_ = _make_initialized_actuator()
        modules["dcgm_agent"].dcgmEntityGetLatestValues.return_value = [
            _make_value(dbl_val=700.0),
        ]
        with patch.dict("sys.modules", {**modules, "pynvml": MagicMock()}):
            result = actuator.default_w(0)
        self.assertEqual(result, 700)
        call = modules["dcgm_agent"].dcgmEntityGetLatestValues.call_args
        self.assertEqual(call.args[3], [163])

    def test_current_w_uses_with_reconnect_on_stale_handle(self):
        """Like constraints_w / get_uuid, current_w wraps its read in
        _with_reconnect. A CONNECTION_NOT_VALID mid-orphan-recovery
        must trigger a rebuild + retry, not abort the agent."""
        actuator, modules, *_ = _make_initialized_actuator()

        DCGMError = modules["dcgm_structs"].DCGMError
        CONNECTION_NOT_VALID = modules["dcgm_structs"].DCGM_ST_CONNECTION_NOT_VALID

        modules["dcgm_agent"].dcgmEntityGetLatestValues.side_effect = [
            DCGMError(CONNECTION_NOT_VALID),
            [_make_value(dbl_val=400.0)],
        ]

        with patch.dict("sys.modules", {**modules, "pynvml": MagicMock()}):
            result = actuator.current_w(0)

        self.assertEqual(result, 400)
        # Reconnect happened.
        self.assertEqual(modules["pydcgm"].DcgmHandle.call_count, 2)

    def test_default_w_uses_with_reconnect_on_stale_handle(self):
        actuator, modules, *_ = _make_initialized_actuator()
        DCGMError = modules["dcgm_structs"].DCGMError
        CONNECTION_NOT_VALID = modules["dcgm_structs"].DCGM_ST_CONNECTION_NOT_VALID

        modules["dcgm_agent"].dcgmEntityGetLatestValues.side_effect = [
            DCGMError(CONNECTION_NOT_VALID),
            [_make_value(dbl_val=700.0)],
        ]

        with patch.dict("sys.modules", {**modules, "pynvml": MagicMock()}):
            result = actuator.default_w(0)

        self.assertEqual(result, 700)
        self.assertEqual(modules["pydcgm"].DcgmHandle.call_count, 2)


# ---------------------------------------------------------------------------
# Write surface — apply_cap
# ---------------------------------------------------------------------------


class TestApplyCap(unittest.TestCase):
    def setUp(self):
        # Reset module-level managed state so each test starts clean.
        power_agent._managed_gpu_indices.clear()
        power_agent._previously_managed.clear()

    def _seed_constraints_and_uuid(self, modules, min_w=100, max_w=700, uuid=b"GPU-x"):
        """dcgmEntityGetLatestValues is called twice per apply_cap:
        once for constraints_w (MIN+MAX → 2 vals), once for get_uuid
        (UUID → 1 val). side_effect chains them in order."""
        modules["dcgm_agent"].dcgmEntityGetLatestValues.side_effect = [
            [_make_value(dbl_val=min_w), _make_value(dbl_val=max_w)],
            [_make_value(str_val=uuid)],
        ]

    def test_apply_cap_requires_metrics(self):
        actuator = DcgmActuator()  # no metrics
        with self.assertRaises(RuntimeError) as ctx:
            actuator.apply_cap(0, 300)
        self.assertIn("metrics", str(ctx.exception).lower())

    def test_apply_cap_happy_path_within_constraints(self):
        metrics = MagicMock()
        actuator, modules, handle, _ = _make_initialized_actuator(metrics=metrics)
        self._seed_constraints_and_uuid(modules)

        with patch.dict("sys.modules", {**modules, "pynvml": MagicMock()}), patch(
            "power_agent._persist_managed_gpus"
        ):
            result = actuator.apply_cap(0, 300)

        self.assertEqual(result, 300)
        # A DcgmGroup was created and the GPU added to it.
        modules["pydcgm"].DcgmGroup.assert_called_once()
        group = modules["pydcgm"].DcgmGroup.return_value
        group.AddGpu.assert_called_once_with(0)
        # The cap write went through grp.config.Set(cfg) with mPowerLimit.val=300.
        group.config.Set.assert_called_once()
        cfg = group.config.Set.call_args.args[0]
        self.assertEqual(cfg.mPowerLimit.val, 300)
        # State tracking parity with the NVML path.
        self.assertIn(0, power_agent._managed_gpu_indices)
        metrics.applied_limit_watts.labels.assert_called_with(gpu="0")
        metrics.applied_limit_watts.labels.return_value.set.assert_called_with(300)
        metrics.apply_failures_total.inc.assert_not_called()

    def test_apply_cap_clamps_above_max(self):
        metrics = MagicMock()
        actuator, modules, *_ = _make_initialized_actuator(metrics=metrics)
        self._seed_constraints_and_uuid(modules, min_w=100, max_w=700)

        with patch.dict("sys.modules", {**modules, "pynvml": MagicMock()}), patch(
            "power_agent._persist_managed_gpus"
        ):
            result = actuator.apply_cap(0, 900)

        self.assertEqual(result, 700)
        cfg = modules["pydcgm"].DcgmGroup.return_value.config.Set.call_args.args[0]
        self.assertEqual(cfg.mPowerLimit.val, 700)
        metrics.cap_clamped_total.labels.assert_called_with(direction="max")

    def test_apply_cap_clamps_below_min(self):
        metrics = MagicMock()
        actuator, modules, *_ = _make_initialized_actuator(metrics=metrics)
        self._seed_constraints_and_uuid(modules, min_w=100, max_w=700)

        with patch.dict("sys.modules", {**modules, "pynvml": MagicMock()}), patch(
            "power_agent._persist_managed_gpus"
        ):
            result = actuator.apply_cap(0, 50)

        self.assertEqual(result, 100)
        metrics.cap_clamped_total.labels.assert_called_with(direction="min")

    def test_apply_cap_enforce_true_calls_enforce(self):
        actuator, modules, *_ = _make_initialized_actuator(
            metrics=MagicMock(), enforce=True
        )
        self._seed_constraints_and_uuid(modules)

        with patch.dict("sys.modules", {**modules, "pynvml": MagicMock()}), patch(
            "power_agent._persist_managed_gpus"
        ):
            actuator.apply_cap(0, 300)

        modules["pydcgm"].DcgmGroup.return_value.config.Enforce.assert_called_once()

    def test_apply_cap_enforce_false_skips_enforce(self):
        """The default — opt-in re-assertion. Set-and-forget like NVML."""
        actuator, modules, *_ = _make_initialized_actuator(
            metrics=MagicMock(), enforce=False
        )
        self._seed_constraints_and_uuid(modules)

        with patch.dict("sys.modules", {**modules, "pynvml": MagicMock()}), patch(
            "power_agent._persist_managed_gpus"
        ):
            actuator.apply_cap(0, 300)

        modules["pydcgm"].DcgmGroup.return_value.config.Enforce.assert_not_called()

    def test_enforce_failure_after_successful_set_still_tracks_managed_state(self):
        """Set OK + Enforce raise → cap MUST still be tracked.

        dcgmConfigSet invokes nvmlDeviceSetPowerManagementLimit inside
        the hostengine, so once Set returns the cap is LIVE on the GPU
        regardless of what Enforce does. Regression-guards the
        Set-OK-Enforce-fail partial-success path: without this test,
        a refactor that re-coupled bookkeeping to Enforce would leave
        a custom cap live with no managed-state tracking — SIGTERM
        wouldn't restore it, orphan recovery
        wouldn't find it.

        Asserts: cap returned, _managed_gpu_indices populated,
        applied_limit_watts set, dcgm_enforce_failures_total
        incremented, apply_failures_total NOT incremented (the cap
        IS live — that metric would mislead operators), no exception
        propagated.
        """
        metrics = MagicMock()
        actuator, modules, *_ = _make_initialized_actuator(
            metrics=metrics, enforce=True
        )
        self._seed_constraints_and_uuid(modules)
        group = modules["pydcgm"].DcgmGroup.return_value
        # Set succeeds; Enforce raises a non-CONNECTION_NOT_VALID error
        # (CONNECTION_NOT_VALID would trigger _with_reconnect recovery
        # which is a different code path, exercised by the stale-handle
        # tests).
        group.config.Enforce.side_effect = modules["dcgm_structs"].DCGMError(
            modules["dcgm_structs"].DCGM_ST_GENERIC_ERROR
        )

        with patch.dict("sys.modules", {**modules, "pynvml": MagicMock()}), patch(
            "power_agent._persist_managed_gpus"
        ):
            result = actuator.apply_cap(0, 300)

        # Cap is live → return value is the post-clamp watts.
        self.assertEqual(result, 300)
        # The GPU IS tracked — the load-bearing assertion: a successful
        # Set means the cap is live on the hardware, so SIGTERM and
        # orphan recovery must find it via _managed_gpu_indices.
        self.assertIn(0, power_agent._managed_gpu_indices)
        # Enforce was attempted (and failed) exactly once.
        group.config.Enforce.assert_called_once()
        # Dedicated metric for "Enforce-only failure" got the tick.
        metrics.dcgm_enforce_failures_total.inc.assert_called_once()
        # apply_failures_total MUST NOT tick — that metric means the
        # cap is NOT live; here the cap IS live (Set succeeded).
        metrics.apply_failures_total.inc.assert_not_called()
        # The applied_limit_watts gauge was set (cap was applied).
        metrics.applied_limit_watts.labels.assert_called_with(gpu="0")
        metrics.applied_limit_watts.labels.return_value.set.assert_called_with(300)

    def test_apply_cap_dcgm_generic_error_bumps_failure_metric_and_returns(self):
        """Non-CONNECTION_NOT_VALID DCGMErrors are non-fatal — the
        reconcile loop logs + continues on to the next GPU."""
        metrics = MagicMock()
        actuator, modules, *_ = _make_initialized_actuator(metrics=metrics)
        modules["dcgm_agent"].dcgmEntityGetLatestValues.side_effect = [
            [_make_value(dbl_val=100), _make_value(dbl_val=700)],
        ]
        group = modules["pydcgm"].DcgmGroup.return_value
        group.config.Set.side_effect = modules["dcgm_structs"].DCGMError(
            modules["dcgm_structs"].DCGM_ST_GENERIC_ERROR
        )

        with patch.dict("sys.modules", {**modules, "pynvml": MagicMock()}):
            result = actuator.apply_cap(0, 300)

        # Returns the *requested* (effective) watts even on failure
        # because downstream callers / Prometheus need a number.
        self.assertEqual(result, 300)
        metrics.apply_failures_total.inc.assert_called_once()
        # GPU is NOT recorded as managed — we don't track an unsuccessful
        # write, otherwise restore-on-shutdown would touch GPUs we never
        # actually capped.
        self.assertNotIn(0, power_agent._managed_gpu_indices)

    def test_apply_cap_blanks_workload_power_profiles(self):
        """Every workload-profile slot MUST be set to DCGM_INT32_BLANK.

        ctypes zero-initializes c_dcgmDeviceConfig_v2, and DCGM's
        config manager treats an all-zero workload-profile array as
        ACTION_CLEAR (see DcgmConfigManagerTests.cpp:207-231 —
        "Initialized target config is cleared by zeroed new config").
        Without the blanking loop in apply_cap, every cap write would
        silently clear whatever workload power profiles were on the
        GPU. This test guards that contract: all 8 slots must be
        BLANK (sentinel 0x7FFFFFF0), never zero.
        """
        actuator, modules, *_ = _make_initialized_actuator(metrics=MagicMock())
        self._seed_constraints_and_uuid(modules)

        with patch.dict("sys.modules", {**modules, "pynvml": MagicMock()}), patch(
            "power_agent._persist_managed_gpus"
        ):
            actuator.apply_cap(0, 300)

        cfg = modules["pydcgm"].DcgmGroup.return_value.config.Set.call_args.args[0]
        blank = modules["dcgm_structs"].DCGM_INT32_BLANK
        size = modules["dcgm_structs"].DCGM_WORKLOAD_POWER_PROFILE_ARRAY_SIZE

        # __setitem__ must have been called once per slot, in index
        # order, each time with the BLANK sentinel. Order matters less
        # than completeness, so we collect the set of (index, value)
        # pairs and compare against the expected coverage.
        setitem_calls = cfg.mWorkloadPowerProfiles.__setitem__.call_args_list
        observed = {call.args[0]: call.args[1] for call in setitem_calls}
        self.assertEqual(
            observed,
            {i: blank for i in range(size)},
            f"Expected every slot in [0, {size}) blanked to {blank:#x}; "
            f"got {observed!r}. An all-zero array would trigger "
            "DCGM ACTION_CLEAR and wipe workload power profiles.",
        )

    def test_apply_cap_caches_group_across_calls(self):
        """Per-GPU DcgmGroup is created once, reused on subsequent
        applies. Saves the DcgmGroup create + AddGpu RPCs per reconcile."""
        actuator, modules, *_ = _make_initialized_actuator(metrics=MagicMock())
        # Two apply_cap calls → 2× constraints + 2× UUID lookups
        modules["dcgm_agent"].dcgmEntityGetLatestValues.side_effect = [
            [_make_value(dbl_val=100), _make_value(dbl_val=700)],
            [_make_value(str_val=b"GPU-x")],
            [_make_value(dbl_val=100), _make_value(dbl_val=700)],
            [_make_value(str_val=b"GPU-x")],
        ]

        with patch.dict("sys.modules", {**modules, "pynvml": MagicMock()}), patch(
            "power_agent._persist_managed_gpus"
        ):
            actuator.apply_cap(0, 300)
            actuator.apply_cap(0, 400)

        # DcgmGroup must be constructed exactly once — second call
        # reuses the cached entry.
        self.assertEqual(modules["pydcgm"].DcgmGroup.call_count, 1)


# ---------------------------------------------------------------------------
# Write surface — restore_default
# ---------------------------------------------------------------------------


class TestRestoreDefault(unittest.TestCase):
    """restore_default MUST read field 163 (DEF), not 162 (MAX).

    This is the v1.4 review fix — DCGM lacks a "reset to default" verb,
    so we read the factory default explicitly. Field 162 is the
    *maximum settable* power limit; on shipped data-center SKUs they're
    numerically equal, but reading 163 ensures correctness on
    hypothetical future SKUs where default < max.
    """

    def setUp(self):
        power_agent._managed_gpu_indices.clear()
        power_agent._previously_managed.clear()

    def test_restore_default_reads_field_163_not_162(self):
        actuator, modules, *_ = _make_initialized_actuator(metrics=MagicMock())
        # Four reads: DEF + constraints (MIN/MAX) + UUID
        modules["dcgm_agent"].dcgmEntityGetLatestValues.side_effect = [
            [_make_value(dbl_val=700.0)],  # restore_default DEF read
            [_make_value(dbl_val=100), _make_value(dbl_val=700)],  # constraints
            [_make_value(str_val=b"GPU-x")],  # uuid for state tracking
        ]

        with patch.dict("sys.modules", {**modules, "pynvml": MagicMock()}), patch(
            "power_agent._persist_managed_gpus"
        ):
            actuator.restore_default(0)

        # First call to dcgmEntityGetLatestValues must request field 163,
        # NOT 162. Guards the latent v1.3 bug.
        first_call_field_ids = (
            modules["dcgm_agent"].dcgmEntityGetLatestValues.call_args_list[0].args[3]
        )
        self.assertEqual(first_call_field_ids, [163])
        # And the cap write was issued at that default.
        cfg = modules["pydcgm"].DcgmGroup.return_value.config.Set.call_args.args[0]
        self.assertEqual(cfg.mPowerLimit.val, 700)

    def test_restore_default_raises_on_write_failure(self):
        """restore_default propagates DCGM write failure to the caller.

        Without this contract, a refactor that re-routed restore_default
        through apply_cap (which absorbs failures into
        apply_failures_total) would let _handle_sigterm log a false-
        positive "restored" message when dcgmConfigSet(default) failed.
        Asserts: DCGMError raised, no bookkeeping side-effects.
        """
        metrics = MagicMock()
        actuator, modules, *_ = _make_initialized_actuator(metrics=metrics)
        modules["dcgm_agent"].dcgmEntityGetLatestValues.side_effect = [
            [_make_value(dbl_val=700.0)],  # default_w field-163 read
        ]
        group = modules["pydcgm"].DcgmGroup.return_value
        DCGMError = modules["dcgm_structs"].DCGMError
        group.config.Set.side_effect = DCGMError(
            modules["dcgm_structs"].DCGM_ST_GENERIC_ERROR
        )

        with patch.dict("sys.modules", {**modules, "pynvml": MagicMock()}):
            with self.assertRaises(DCGMError):
                actuator.restore_default(0)

        # The exception path must NOT update managed-state — pre-v1.7
        # restore_default would have left _managed_gpu_indices intact
        # via apply_cap's success-path bookkeeping; the new path skips
        # bookkeeping because _apply_cap_inner raises before it runs.
        self.assertNotIn(0, power_agent._managed_gpu_indices)
        metrics.applied_limit_watts.labels.assert_not_called()


# ---------------------------------------------------------------------------
# Stale-handle recovery (_with_reconnect)
# ---------------------------------------------------------------------------


class TestStaleHandleRecovery(unittest.TestCase):
    def test_connection_not_valid_triggers_single_retry(self):
        """First op() raises CONNECTION_NOT_VALID; second op() succeeds.
        Production code must rebuild the handle, flush groups, retry."""
        modules = _make_dcgm_modules()
        _wire_handle(modules["pydcgm"], gpu_ids=(0, 1))
        nvml = MagicMock()
        actuator = DcgmActuator(metrics=MagicMock())

        with patch.dict("sys.modules", {**modules, "pynvml": nvml}):
            actuator.init()

            # Seed a cached group so we can assert it's flushed.
            actuator._groups[0] = MagicMock()

            DCGMError = modules["dcgm_structs"].DCGMError
            CONNECTION_NOT_VALID = modules["dcgm_structs"].DCGM_ST_CONNECTION_NOT_VALID

            call_count = {"n": 0}

            def op():
                call_count["n"] += 1
                if call_count["n"] == 1:
                    raise DCGMError(CONNECTION_NOT_VALID)
                return "ok"

            result = actuator._with_reconnect(op)

        self.assertEqual(result, "ok")
        self.assertEqual(call_count["n"], 2)
        # Groups must have been cleared during recovery — caching a
        # stale-handle group would re-raise on the next reconcile.
        self.assertEqual(actuator._groups, {})
        # DcgmHandle constructor was called twice: once in init(), once
        # during recovery in _with_reconnect.
        self.assertEqual(modules["pydcgm"].DcgmHandle.call_count, 2)

    def test_other_dcgm_errors_propagate_without_retry(self):
        """A NOT_SUPPORTED / GENERIC_ERROR / etc. must propagate
        immediately — recovering would mask the wrong class of failure."""
        modules = _make_dcgm_modules()
        _wire_handle(modules["pydcgm"])
        nvml = MagicMock()
        actuator = DcgmActuator(metrics=MagicMock())

        with patch.dict("sys.modules", {**modules, "pynvml": nvml}):
            actuator.init()

            DCGMError = modules["dcgm_structs"].DCGMError
            GENERIC_ERROR = modules["dcgm_structs"].DCGM_ST_GENERIC_ERROR

            calls = {"n": 0}

            def op():
                calls["n"] += 1
                raise DCGMError(GENERIC_ERROR)

            with self.assertRaises(DCGMError):
                actuator._with_reconnect(op)

        # Exactly one attempt — no retry, no reconnect.
        self.assertEqual(calls["n"], 1)
        self.assertEqual(modules["pydcgm"].DcgmHandle.call_count, 1)

    def test_persistent_connection_failure_propagates_after_one_retry(self):
        """If reconnect succeeds but op() raises CONNECTION_NOT_VALID
        again, the second exception propagates — no infinite retry."""
        modules = _make_dcgm_modules()
        _wire_handle(modules["pydcgm"])
        nvml = MagicMock()
        actuator = DcgmActuator(metrics=MagicMock())

        with patch.dict("sys.modules", {**modules, "pynvml": nvml}):
            actuator.init()

            DCGMError = modules["dcgm_structs"].DCGMError
            CONNECTION_NOT_VALID = modules["dcgm_structs"].DCGM_ST_CONNECTION_NOT_VALID

            calls = {"n": 0}

            def op():
                calls["n"] += 1
                raise DCGMError(CONNECTION_NOT_VALID)

            with self.assertRaises(DCGMError):
                actuator._with_reconnect(op)

        self.assertEqual(calls["n"], 2)  # one initial + one retry

    def test_non_dcgm_exception_propagates_immediately(self):
        """Random Python exceptions (not DCGMError subclasses) must
        also pass through — _with_reconnect is DCGM-specific."""
        actuator, modules, *_ = _make_initialized_actuator(metrics=MagicMock())
        with patch.dict("sys.modules", {**modules, "pynvml": MagicMock()}):
            with self.assertRaises(ValueError):
                actuator._with_reconnect(
                    lambda: (_ for _ in ()).throw(ValueError("nope"))
                )


if __name__ == "__main__":
    unittest.main()
