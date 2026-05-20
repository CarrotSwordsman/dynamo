# SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the SIGTERM handler.

v1.6 wiring (per review comment #6): `_handle_sigterm` now dispatches
through the module-level `_active_actuator` (set by
`PowerAgent.__init__`) instead of going straight to `pynvml`. This is
required so that on `actuator: dcgm` the cap restore flows through the
hostengine and DCGM's target-config record stays consistent with the
driver-level cap (otherwise DCGM's reset/reinit auto-reapply would
silently re-push the *old* cap, contradicting the SIGTERM intent).

This file covers three scenarios:
  1. With an actuator registered — `restore_default` and `shutdown` are
     called on the actuator, never directly via pynvml.
  2. Without an actuator registered (defensive fallback) — raw NVML
     restore runs so the GPU isn't left at a custom cap.
  3. Edge cases: no managed GPUs, actuator.restore_default raising,
     _shutdown event still fires.
"""

import signal
import unittest
from unittest.mock import MagicMock, patch

import power_agent


class _SigtermTestBase(unittest.TestCase):
    def setUp(self):
        # Reset module-level state between tests so each starts from a
        # known clean slate (some prior test may have left _active_actuator
        # populated).
        power_agent._managed_gpu_indices.clear()
        power_agent._shutdown.clear()
        self._saved_active = power_agent._active_actuator
        power_agent._active_actuator = None

    def tearDown(self):
        power_agent._active_actuator = self._saved_active
        power_agent._managed_gpu_indices.clear()
        power_agent._shutdown.clear()


class TestSigtermViaActuator(_SigtermTestBase):
    """v1.6 happy path — actuator registered, dispatch via it."""

    def test_restores_default_via_actuator_for_each_managed_gpu(self):
        power_agent._managed_gpu_indices.update([0, 1, 2])
        actuator = MagicMock()
        actuator.name = "nvml"
        power_agent._active_actuator = actuator

        # pynvml should be untouched on this path.
        mock_nvml = MagicMock()
        with patch.object(power_agent, "pynvml", mock_nvml):
            power_agent._handle_sigterm(signal.SIGTERM, None)

        # Restore was dispatched to the actuator for each managed GPU.
        self.assertEqual(actuator.restore_default.call_count, 3)
        actuator.restore_default.assert_any_call(0)
        actuator.restore_default.assert_any_call(1)
        actuator.restore_default.assert_any_call(2)
        # Shutdown was dispatched to the actuator (not raw nvmlShutdown).
        actuator.shutdown.assert_called_once()
        mock_nvml.nvmlShutdown.assert_not_called()
        mock_nvml.nvmlDeviceSetPowerManagementLimit.assert_not_called()
        self.assertTrue(power_agent._shutdown.is_set())

    def test_dcgm_actuator_routes_restore_through_dcgm_not_nvml(self):
        """The load-bearing test for review comment #6.

        Pre-v1.6, SIGTERM on `actuator: dcgm` used raw NVML, leaving the
        DCGM hostengine's target-config record stale. This test verifies
        the dispatch now goes through the DCGM actuator surface — so the
        actual production code will issue `dcgmConfigSet(default_w)` and
        keep target-config in sync.
        """
        power_agent._managed_gpu_indices.add(7)
        dcgm_actuator = MagicMock()
        dcgm_actuator.name = "dcgm"
        power_agent._active_actuator = dcgm_actuator

        mock_nvml = MagicMock()
        with patch.object(power_agent, "pynvml", mock_nvml):
            power_agent._handle_sigterm(signal.SIGTERM, None)

        dcgm_actuator.restore_default.assert_called_once_with(7)
        dcgm_actuator.shutdown.assert_called_once()
        # CRITICAL: raw NVML must NEVER be called when an actuator is
        # registered. If this fires, comment #6 has regressed.
        mock_nvml.nvmlDeviceSetPowerManagementLimit.assert_not_called()
        mock_nvml.nvmlShutdown.assert_not_called()

    def test_actuator_restore_failure_does_not_prevent_shutdown(self):
        """A failing restore on one GPU must not stop the loop or
        prevent actuator.shutdown() from being called."""
        power_agent._managed_gpu_indices.update([0, 1])
        actuator = MagicMock()
        actuator.name = "dcgm"
        actuator.restore_default.side_effect = [
            RuntimeError("DCGM down"),
            None,  # GPU 1 succeeds
        ]
        power_agent._active_actuator = actuator

        with patch.object(power_agent, "pynvml", MagicMock()):
            power_agent._handle_sigterm(signal.SIGTERM, None)

        # Both GPUs were attempted.
        self.assertEqual(actuator.restore_default.call_count, 2)
        # Shutdown still ran despite the per-GPU failure.
        actuator.shutdown.assert_called_once()
        self.assertTrue(power_agent._shutdown.is_set())

    def test_actuator_shutdown_failure_does_not_prevent_shutdown_event(self):
        """Even if actuator.shutdown() raises (e.g. DCGM hostengine
        gone), the _shutdown event must still be set so the main loop
        can exit. Otherwise the agent would hang on SIGTERM."""
        power_agent._managed_gpu_indices.add(0)
        actuator = MagicMock()
        actuator.name = "dcgm"
        actuator.shutdown.side_effect = RuntimeError("hostengine gone")
        power_agent._active_actuator = actuator

        with patch.object(power_agent, "pynvml", MagicMock()):
            power_agent._handle_sigterm(signal.SIGTERM, None)

        self.assertTrue(power_agent._shutdown.is_set())

    def test_actuator_shutdown_failure_is_logged_not_silently_swallowed(self):
        """Per PR #9682 CodeRabbit review on power_agent.py:355.

        Pre-fix the shutdown try/except swallowed failures silently
        (`except Exception: pass`), so a DCGM hostengine fault or NVML
        teardown error at SIGTERM time was invisible in pod logs.
        We still don't re-raise (the agent MUST exit cleanly) but the
        failure now writes a traceback at ERROR level so operators
        can correlate with hostengine / driver events post-mortem.
        """
        power_agent._managed_gpu_indices.add(0)
        actuator = MagicMock()
        actuator.name = "dcgm"
        actuator.shutdown.side_effect = RuntimeError("hostengine gone")
        power_agent._active_actuator = actuator

        with patch.object(power_agent, "pynvml", MagicMock()):
            with self.assertLogs("power_agent", level="ERROR") as cm:
                power_agent._handle_sigterm(signal.SIGTERM, None)

        # The original RuntimeError message must appear in the log
        # output (logger.exception attaches the traceback).
        joined = "\n".join(cm.output)
        self.assertIn("hostengine gone", joined)
        # Sanity: shutdown event still fires.
        self.assertTrue(power_agent._shutdown.is_set())

    def test_raw_nvml_shutdown_failure_is_logged(self):
        """Defensive-fallback path: when no actuator is registered and
        `pynvml.nvmlShutdown()` itself raises, the failure must be
        logged (was silently swallowed pre-fix)."""
        # No managed GPUs so we skip the per-GPU restore loop and go
        # straight to the actuator-or-NVML shutdown branch.
        mock_nvml = MagicMock()
        mock_nvml.nvmlShutdown.side_effect = RuntimeError(
            "NVML library gone (driver unloaded)"
        )

        with patch.object(power_agent, "pynvml", mock_nvml):
            with self.assertLogs("power_agent", level="ERROR") as cm:
                power_agent._handle_sigterm(signal.SIGTERM, None)

        joined = "\n".join(cm.output)
        self.assertIn("NVML library gone", joined)
        self.assertTrue(power_agent._shutdown.is_set())


class TestSigtermFallback(_SigtermTestBase):
    """Defensive fallback — actuator not yet registered.

    Should only happen if SIGTERM fires during PowerAgent.__init__ before
    `_active_actuator = self._actuator` runs. We still want a sane
    restore on those rare timings, so the handler falls through to raw
    NVML rather than abandoning a cap'd GPU.
    """

    def test_no_actuator_restores_via_raw_nvml(self):
        power_agent._managed_gpu_indices.update([0, 1, 2])
        # _active_actuator stays None per _SigtermTestBase.setUp.
        mock_nvml = MagicMock()
        mock_nvml.nvmlDeviceGetHandleByIndex.side_effect = lambda i: f"handle_{i}"
        mock_nvml.nvmlDeviceGetPowerManagementDefaultLimit.return_value = 700_000
        mock_nvml.NVMLError = Exception

        with patch.dict("sys.modules", {"pynvml": mock_nvml}):
            with patch.object(power_agent, "pynvml", mock_nvml):
                power_agent._handle_sigterm(signal.SIGTERM, None)

        # Pre-v1.6 behaviour preserved as the fallback path.
        self.assertEqual(mock_nvml.nvmlDeviceSetPowerManagementLimit.call_count, 3)
        mock_nvml.nvmlDeviceSetPowerManagementLimit.assert_any_call("handle_0", 700_000)
        mock_nvml.nvmlDeviceSetPowerManagementLimit.assert_any_call("handle_1", 700_000)
        mock_nvml.nvmlDeviceSetPowerManagementLimit.assert_any_call("handle_2", 700_000)
        mock_nvml.nvmlShutdown.assert_called_once()
        self.assertTrue(power_agent._shutdown.is_set())

    def test_no_managed_gpus_still_shuts_down(self):
        # Neither actuator nor managed GPUs.
        mock_nvml = MagicMock()
        with patch.object(power_agent, "pynvml", mock_nvml):
            power_agent._handle_sigterm(signal.SIGTERM, None)

        mock_nvml.nvmlDeviceSetPowerManagementLimit.assert_not_called()
        mock_nvml.nvmlShutdown.assert_called_once()
        self.assertTrue(power_agent._shutdown.is_set())

    def test_nvml_error_does_not_prevent_shutdown(self):
        power_agent._managed_gpu_indices.add(0)
        mock_nvml = MagicMock()
        mock_nvml.nvmlDeviceGetHandleByIndex.side_effect = Exception("nvml error")
        mock_nvml.NVMLError = Exception

        with patch.object(power_agent, "pynvml", mock_nvml):
            power_agent._handle_sigterm(signal.SIGTERM, None)

        mock_nvml.nvmlShutdown.assert_called_once()
        self.assertTrue(power_agent._shutdown.is_set())


if __name__ == "__main__":
    unittest.main()
