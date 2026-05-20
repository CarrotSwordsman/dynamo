# SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the v1.6 actuator wiring in `PowerAgent._reconcile_gpu`.

Pre-v1.6 the reconcile loop hard-coded `pynvml.nvmlDeviceGetHandleByIndex`
and the module-level `_apply_cap(handle, ...)` — so `agent.actuator=dcgm`
only affected cold-start orphan recovery; steady-state cap writes silently
used NVML regardless of actuator selection. That was the load-bearing bug
in review comment #4.

v1.6 routes `_reconcile_gpu` through `self._actuator.list_running_pids`
and `self._actuator.apply_cap`. This file pins that contract:

  - PID enumeration goes through the actuator (so DcgmActuator's UUID-
    keyed identity map runs and PID reads land on the correct physical
    GPU).
  - Cap writes go through the actuator (so DcgmActuator's
    `dcgmConfigSet` actually runs on the DCGM path).
  - Raw `pynvml.nvmlDeviceGetHandleByIndex` is NOT called from
    `_reconcile_gpu` — if a future refactor reintroduces it, this test
    will fail and surface the regression.
  - Early-exit branches (no PIDs / no K8s PIDs) preserve their no-op
    behaviour.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import power_agent
from power_agent import PowerAgent


def _make_agent_with_actuator(actuator):
    """Build a PowerAgent without exercising __init__'s NVML/K8s deps."""
    agent = object.__new__(PowerAgent)
    agent._actuator = actuator
    agent.metrics = MagicMock()
    agent.safe_default_watts = 500
    agent.device_count = 1
    return agent


class TestReconcileGpuRoutesViaActuator(unittest.TestCase):
    """The four wiring guarantees from v1.6."""

    def setUp(self):
        power_agent._managed_gpu_indices.clear()

    def test_pid_enumeration_goes_through_actuator(self):
        """list_running_pids must be called on self._actuator, not pynvml."""
        actuator = MagicMock()
        actuator.list_running_pids.return_value = []  # no workload, early exit
        agent = _make_agent_with_actuator(actuator)

        mock_nvml = MagicMock()
        with patch.object(power_agent, "pynvml", mock_nvml):
            agent._reconcile_gpu(3, {})

        actuator.list_running_pids.assert_called_once_with(3)
        # CRITICAL: raw NVML must NOT be touched for PID enumeration.
        # Pre-v1.6 the inline path called nvmlDeviceGetHandleByIndex +
        # nvmlDeviceGetComputeRunningProcesses; if either re-appears the
        # actuator's UUID-keyed identity map is bypassed and PID reads
        # land on the wrong physical GPU.
        mock_nvml.nvmlDeviceGetHandleByIndex.assert_not_called()
        mock_nvml.nvmlDeviceGetComputeRunningProcesses.assert_not_called()

    def test_no_pids_skips_apply_cap(self):
        actuator = MagicMock()
        actuator.list_running_pids.return_value = []
        agent = _make_agent_with_actuator(actuator)

        with patch.object(power_agent, "pynvml", MagicMock()):
            agent._reconcile_gpu(0, {})

        actuator.apply_cap.assert_not_called()

    def test_non_k8s_pids_skip_apply_cap(self):
        """PIDs that don't resolve to a pod UID (e.g. host daemons) don't
        trigger an apply_cap. Same behaviour as pre-v1.6."""
        actuator = MagicMock()
        actuator.list_running_pids.return_value = [9999]
        agent = _make_agent_with_actuator(actuator)

        with patch.object(power_agent, "pynvml", MagicMock()):
            with patch("power_agent._extract_pod_uid_from_cgroup", return_value=None):
                agent._reconcile_gpu(0, {"some-uid": "300"})

        actuator.apply_cap.assert_not_called()

    def test_apply_cap_goes_through_actuator_with_resolved_watts(self):
        """The load-bearing test for review comment #4.

        Pre-v1.6 the cap write was `_apply_cap(handle, gpu_idx, cap_w, metrics)`
        — module-level NVML, ignoring `self._actuator`. Setting
        `agent.actuator=dcgm` was therefore a no-op for the steady-state
        cap path. This test pins the v1.6 contract: the actuator
        receives the apply_cap call directly.
        """
        actuator = MagicMock()
        actuator.list_running_pids.return_value = [1234]
        agent = _make_agent_with_actuator(actuator)

        with patch.object(power_agent, "pynvml", MagicMock()):
            with patch(
                "power_agent._extract_pod_uid_from_cgroup",
                return_value="pod-uid-1",
            ):
                agent._reconcile_gpu(2, {"pod-uid-1": "350"})

        actuator.apply_cap.assert_called_once_with(2, 350)
        # No module-level _apply_cap, no raw NVML write.
        # (We can't easily assert _apply_cap wasn't called because it's
        # a module function, but the actuator.apply_cap call is the
        # positive proof — if _apply_cap had run too, the test for
        # comment #8 (double clamp) would catch the doubling.)

    def test_safe_default_used_when_annotation_is_none(self):
        """Pod has no annotation → safe_default_watts via actuator."""
        actuator = MagicMock()
        actuator.list_running_pids.return_value = [1234]
        agent = _make_agent_with_actuator(actuator)
        agent.safe_default_watts = 450

        with patch.object(power_agent, "pynvml", MagicMock()):
            with patch(
                "power_agent._extract_pod_uid_from_cgroup",
                return_value="pod-uid-1",
            ):
                agent._reconcile_gpu(0, {"pod-uid-1": None})

        actuator.apply_cap.assert_called_once_with(0, 450)


class TestReconcileGpuPolicyResolution(unittest.TestCase):
    """The v1.6 wiring must preserve the multi-pod-per-GPU resolution
    contract (`_resolve_cap_for_gpu`). Two pods that agree → use that
    value; two pods that disagree → safe default."""

    def setUp(self):
        power_agent._managed_gpu_indices.clear()

    def test_two_pods_agree(self):
        actuator = MagicMock()
        actuator.list_running_pids.return_value = [1111, 2222]
        agent = _make_agent_with_actuator(actuator)

        def cgroup(pid):
            return {1111: "pod-A", 2222: "pod-B"}[pid]

        with patch.object(power_agent, "pynvml", MagicMock()):
            with patch("power_agent._extract_pod_uid_from_cgroup", side_effect=cgroup):
                agent._reconcile_gpu(0, {"pod-A": "400", "pod-B": "400"})

        actuator.apply_cap.assert_called_once_with(0, 400)

    def test_two_pods_disagree_uses_safe_default(self):
        actuator = MagicMock()
        actuator.list_running_pids.return_value = [1111, 2222]
        agent = _make_agent_with_actuator(actuator)
        agent.safe_default_watts = 500

        def cgroup(pid):
            return {1111: "pod-A", 2222: "pod-B"}[pid]

        with patch.object(power_agent, "pynvml", MagicMock()):
            with patch("power_agent._extract_pod_uid_from_cgroup", side_effect=cgroup):
                agent._reconcile_gpu(0, {"pod-A": "300", "pod-B": "600"})

        actuator.apply_cap.assert_called_once_with(0, 500)


if __name__ == "__main__":
    unittest.main()
