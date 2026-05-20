# Power Agent — Dual Actuator (NVML + DCGM)

**Status:** Implementation in flight. Initially drafted 2026-05-19 as a follow-up to the PR9682 reviewer ask ("add DCGM actuation path"). Adds a second actuation path (DCGM, opt-in via `agent.actuator=dcgm`) on top of the NVML-only Power Agent that shipped in PR9682.
**Author:** Kai Ma

### Revision history

| Rev | Date | Changes |
|-----|------|---------|
| v1 | 2026-05-19 | Initial draft after field-engineer report that some customer sites set `dcgm.enabled=true` in their GPU Operator install. Promotes v2.4 §6 ("if we ever revisit") from speculative recovery plan to active design. Two-path actuator (NVML default, DCGM opt-in). |
| v1.1 | 2026-05-19 | Author lock-ins after first review pass: `auto` Helm default + full-DCGM on the DCGM path + ctypes decode of `DCGM_FI_DEV_COMPUTE_PIDS`. **Both decisions reversed in v1.2** — see v1.2 entry for why. v1.1 is kept in the history as a record of the path not taken. |
| v1.3 | 2026-05-19 | Resolves all four remaining open questions. **Q2 (substantive):** flip `--dcgm-enforce` default from `true` to `false`. Rationale: set-and-forget matches NVML's semantics, so an NVML→DCGM customer gets the *same* observable cap-write behaviour by default and opts in to the re-assertion property when they want it. With `enforce: false` the default DCGM path differs from NVML only by audit-log emission and writer-path routing (single-writer-through-nvidia-dcgm, §6.6), not by resilience. Customers who specifically want survival of external `nvidia-smi -pl` or Power Agent restarts set `enforce: true`. This also reduces hostengine CPU on the default path. **Q4 (recommendation stood):** per-GPU `DcgmGroup` for config writes. **Q5 (recommendation stood):** `restore_default` uses `mPowerLimit.val = max_w`. **Q6 (recommendation stood):** vendor pydcgm into the existing image (~5 MB) rather than rebasing on `nvcr.io/nvidia/cloud-native/dcgm` (+400 MB). §7 attribution table updated to reflect the new default — the "cap survives external clobbering" row now reads "Yes *if* `enforce: true` (opt-in); otherwise no (matches NVML)." §13 summary softens the DCGM-buys-resilience claim to be opt-in. |
| v1.10 | 2026-05-20 | **First in-cluster DCGM e2e parity run on an 8×A100 SXM node** — three production defects discovered, all fixed. Pre-v1.10 the DCGM path had only ever been exercised against fully-mocked pydcgm in unit tests; the first run against a real `nv-hostengine 4.5.3` failed at the first identity-map build (`DcgmActuator: 8 GPU UUID(s) visible to DCGM are not visible to NVML in this process: ['<<<NULL>>>', ...]`). Root-causing showed two distinct latent bugs and one wrong import path, all in `DcgmActuator`. **#1 (UUID read returns blank sentinel):** `get_uuid` and `_ensure_identity_map._read_dcgm_uuids` read `DCGM_FI_DEV_UUID` (54) via `dcgmEntityGetLatestValues`. That API returns the latest *cached* value from DCGM's field cache, which is only populated when *some* DCGM consumer has previously subscribed via `dcgmWatchFields`. On a freshly-started hostengine with no companion watcher (our test rig; any production cluster whose `nvidia-dcgm-exporter` configuration doesn't watch UUID — the upstream default `dcp-metrics-included.csv` doesn't), the cache returns the string-blank sentinel `DCGM_STR_BLANK = "<<<NULL>>>"` (`/shared/pydcgm/dcgmvalue.py:24`). v1.10 routes UUID reads through `DcgmSystem.discovery.GetGpuAttributes(gpu_id).identifiers.uuid` — the synchronous device-info API that wraps `dcgmGetDeviceAttributes`, returning a `c_dcgmDeviceAttributes_v3` struct populated from the hostengine's discovery state without depending on the field cache. Confirmed against the e2e rig: NULL × 8 from the field-cache path, real UUIDs × 8 from `GetGpuAttributes`, matching NVML byte-for-byte. **#2 (power-limit reads return float blank sentinel):** same root cause hits `constraints_w` / `current_w` / `default_w`, all four of which read `DCGM_FI_DEV_POWER_MGMT_LIMIT{,_MIN,_MAX,_DEF}` (160/161/162/163) from the field cache. Probe confirmed every field returns `DCGM_FP64_BLANK = 140737488355328.0` (= 2^47); `apply_cap`'s clamp then escalated the requested 250 W *up* to the blank max_w and tried to write 2^47 W. v1.10 consolidates all four reads onto a new `_power_limits(gpu_idx)` helper that returns `GetGpuAttributes(gpu_id).powerLimits` (a `c_dcgmDevicePowerLimits_v1` struct with `curPowerLimit`, `defaultPowerLimit`, `enforcedPowerLimit`, `minPowerLimit`, `maxPowerLimit`, all in watts). One RPC instead of four, no field-cache dependency, integer watts directly (no `int(vals[0].value.dbl)` conversion). E2e probe values (`min=100, max=400, default=400`) match NVML byte-for-byte. **#3 (DCGM_INT32_BLANK in wrong module):** `apply_cap`'s workload-power-profile blanking loop reached for `dcgm_structs.DCGM_INT32_BLANK`; that constant lives in `dcgmvalue` (NOT `dcgm_structs`) per the actual pydcgm bindings — `apt-get install datacenter-gpu-manager-4-{core,cuda12}` lays it at `/shared/pydcgm/dcgmvalue.py:17`. Pre-v1.10 the first cap write raised `AttributeError: module 'dcgm_structs' has no attribute 'DCGM_INT32_BLANK'`. Fixed by adding `import dcgmvalue` and swapping all seven references. **Test infrastructure:** `_make_dcgm_modules` gains a `dcgmvalue` MagicMock with all four blank sentinels; `_make_gpu_attrs` extended to carry both `.identifiers.uuid` and `.powerLimits.{cur,default,enforced,min,max}PowerLimit`; `_wire_handle` wires `GetGpuAttributes(gid)` per-gpu_id; `_seed_constraints_and_uuid` simplified onto the unified GetGpuAttributes path. Added `modules["dcgm_agent"].dcgmEntityGetLatestValues.assert_not_called()` regression guards in 5 places — locks in "no more silent field-cache reads of static device info." 163/163 power-agent unit tests pass; pre-commit (black/flake8/codespell/ruff/EOF) clean. **E2e re-validation:** with v1.10 in place, the same 8×A100 rig produces `PASS: NvmlActuator and DcgmActuator agree on all probes (tolerance ±2.0 W)`, exit code 0, with `nvidia-smi` confirming `apply_cap(250)` → 250 W and `restore_default()` → 400 W on every GPU. |
| v1.9 | 2026-05-20 | Fourth auto-reviewer pass — five findings, four accepted (#1 carryover). **#2 (Helm unittest broken):** `validate_actuator_test.yaml` and `validate_enforce_test.yaml` applied `matchRegex`/`notMatchRegex` to `spec.template.spec.containers[0].command`, which renders as a YAML list — helm-unittest 1.1.0 errors with `expect 'spec.template.spec.containers[0].command' to be a string`. Verified against the locally-installed plugin: pre-v1.9 was 8 failed / 16 passed, exactly matching the reviewer's count. Rewrote all 11 `matchRegex` assertions as `contains` (exact element match — stricter than the regex form, catches accidental flag-value drift) and all `notMatchRegex` cases as one-or-more `notContains` against each renderable value. New count: 24/24 passed. **#3 (apply_failures_total semantics):** `_resolve_cap_for_gpu` was incrementing `apply_failures_total.inc()` on policy-fallback paths (no parseable annotation / parse error) at `power_agent.py:442` and `:477`, but the subsequent `actuator.apply_cap(safe_default_watts)` makes the cap LIVE at safe-default value — so v1.8's HELP rewrite ("cap NOT live") was inconsistent. v1.9 removes both policy-path `inc()` calls and rewords the HELP to be precise: "Times an actuator write (NVML `nvmlDeviceSetPowerManagementLimit` or DCGM `dcgmConfigSet`) raised — cap was NOT applied. Distinct from policy fallbacks (tracked by `safe_default_applied_total`) where the cap IS applied at safe-default." `test_multi_pod_policy.py::test_no_parseable_annotation` and `::test_invalid_annotation_value` now assert `apply_failures == 0` (was `== 1`) — operator dashboards / alerts that fire on `apply_failures_total` now signal unambiguously "actuator failed, cap is gone," not "we couldn't parse an annotation." **#4 (design doc apply_cap pseudo-code drift):** §6.3 `apply_cap` sample (line 668) still showed the pre-v1.8 monolithic Set+Enforce-then-return-effective_w structure — the changelog explained the v1.8 split but the implementation sample still taught the bug. Rewrote the sample to show the actual v1.8 production structure: thin `apply_cap` (clamp + try/except absorption) → `_apply_cap_inner` (Set → `_record_managed_state` → optional Enforce-with-soft-failure) → `_record_managed_state` helper. **#5 (README values table drift):** detailed dev-mode section was fixed in v1.7 and v1.8 but the compact values table at `README.md:252-253` still said `dev.scriptConfigMap` holds only `power_agent.py` and `dev.image` only mentioned vllm-runtime/pynvml. Both rows now spell out the v1.7 `actuator.py` requirement and the v1.8 DCGM dev-image override inline. **#1 (carryover):** new files still untracked — user stages them. |
| v1.8 | 2026-05-20 | Third auto-reviewer pass — five findings, four accepted (#1 carryover). **#2 (Set-OK-Enforce-fail untracked cap):** when `enforce: true`, if `dcgmConfigSet` succeeded but the follow-up `dcgmConfigEnforce` raised, `_apply_cap_inner` exited before the managed-state bookkeeping. The cap was LIVE on the GPU (DCGM invokes `nvmlDeviceSetPowerManagementLimit` synchronously inside Set) but `_managed_gpu_indices` didn't contain `gpu_idx`, the persistent UUID file wasn't updated — SIGTERM wouldn't restore it, next-startup orphan recovery wouldn't find it. v1.8 splits `_apply_cap_inner` into Set + bookkeeping + optional Enforce so Set-success unconditionally records managed state; Enforce-only failure is now a soft path that logs + ticks a new `dynamo_power_agent_dcgm_enforce_failures_total` counter (distinct from `apply_failures_total` because the cap IS live). New `test_enforce_failure_after_successful_set_still_tracks_managed_state` locks the contract. **#3 (DCGM dev mode dependency-incomplete):** README claimed `--set agent.actuator=dcgm` was sufficient for dev mode, but `dev.image` defaults to `vllm-runtime:1.0.1` which ships `pynvml` only. README + `values.yaml` now spell out the required `--set dev.image.repository=nvcr.io/nvidia/dynamo/power-agent --set dev.image.tag=v1.1.0` override (the prod image vendors pydcgm + libdcgm.so + has `PYTHONPATH`/`LD_LIBRARY_PATH` baked in). Script-iteration via ConfigMap mount still works because the dev-pod command invokes `/scripts/power_agent.py` explicitly. **#4 (stale pseudo-code drift):** three line edits in `power-agent-dual-actuator.md` so the §6/§9/§10 samples match the v1.7 production code — `restore_default` now shows `_apply_cap_inner` (not `apply_cap`), `--dcgm-enforce` shows `_parse_bool_strict` (not the v1.5 permissive lambda), and §9 says `/opt/dcgm/python` + `/opt/dcgm/lib` (matching Dockerfile:66-71) instead of the v1.0 `/opt/pydcgm/` placeholder. **#5 (observability NVML-specific):** Prometheus HELP strings in `PowerAgentMetrics` rewritten to actuator-neutral wording ("cap currently applied", "cap was NOT live"), and `NOTES.txt` now lists both `apply_failures_total` and the new `dcgm_enforce_failures_total` with semantic labels. **#1 (carryover):** new files still untracked — user stages them as separate logical commits. |
| v1.7 | 2026-05-20 | Second auto-reviewer pass on the PR — seven findings, all accepted. **#4 (SIGTERM honesty):** `DcgmActuator.restore_default` now bypasses `apply_cap`'s failure-absorption (extracted `_apply_cap_inner` raises on DCGM write failure) so `_handle_sigterm`'s success log isn't a false-positive when `dcgmConfigSet(default)` fails. `apply_cap` contract unchanged. **#3 (reconnect coverage):** `_ensure_identity_map`'s DCGM UUID-read loop wrapped in `_with_reconnect` so first-call recovery folds into the same single-retry pattern every other DCGM read uses. **#2 (dev-mode):** dev-pod ConfigMap now mounts `actuator.py` alongside `power_agent.py`, and the dev-pod command line now plumbs `--actuator`/`--dcgm-host`/`--dcgm-port`/`--dcgm-enforce` (pre-v1.7 dev pods silently defaulted to NVML regardless of `agent.actuator=dcgm`). **#7 (template-time typo safety):** new `validateEnforce` helper rejects `--set agent.dcgm.enforce=treu` at `helm install` time (mirrors `_parse_bool_strict`'s allowlist). **#5 (doc fidelity):** corrected three stale samples in §6.1/§6.3/§10.2 (UUID-persistence ownership, UUID-keyed PID routing, `_make_actuator` metrics parameter). **#6 (version pinning):** install-example `image.tag` bumped `v1.0.0 → v1.1.0` in four READMEs to match the chart's `appVersion` (a v1.0.0 image lacks `actuator.py` and rejects the new CLI flags). **#1 (carryover):** new files still untracked — user stages them as separate logical commits. New regressions: `test_restore_default_raises_on_write_failure`, `test_identity_map_build_recovers_from_connection_not_valid`, and `validate_enforce_test.yaml` (8 valid + 4 typo classes + cross-validator interaction). |
| v1.6 | 2026-05-20 | Auto-reviewer pass — eleven findings, six accepts, two partial accepts, three rejected/reframed. **Wiring (accept #4 + #6):** PR A had built `self._actuator` but `_reconcile_gpu` and `_handle_sigterm` still hard-coded `pynvml`, so `agent.actuator=dcgm` only affected cold-start orphan recovery. v1.6 routes both through the actuator surface: `_reconcile_gpu` calls `self._actuator.list_running_pids` + `self._actuator.apply_cap`; `_handle_sigterm` dispatches via a new module-level `_active_actuator` (set in `PowerAgent.__init__`) so the signal callback can reach the actuator. SIGTERM now calls `actuator.restore_default` + `actuator.shutdown` so DCGM's "target configuration" record stays in sync with the driver-level cap on `enforce: true`. New `test_reconcile_wiring.py` (7 tests) + rewritten `test_shutdown.py` (7 tests, both actuator-dispatch and defensive fallback) lock the contract. **Doc fidelity (accept #2 + #3 + #7):** §10.3 row 2's "~1-s re-assert" claim — already corrected in §3/§4.1/§5.3/§7/§13 in v1.5 but missed in §10.3 — now reframed honestly. TL;DR §1.2 wording about "reconciler unchanged" softened to "observably unchanged on NVML path; dispatch is now via actuator." `values.yaml`, `README.md`, and Chart.yaml inherited the stale 1-s framing from v1.x — all three updated. **Correctness (accept #8):** `NvmlActuator.apply_cap` was calling `_clamp_to_constraints` directly *and* delegating to `_apply_cap` (which clamps internally), so out-of-range requests double-logged the clamp warning and double-incremented `cap_clamped_total`. Single clamp now; new regression test `test_apply_cap_clamp_fires_exactly_once_on_out_of_range`. **Usability (accept #10):** `--dcgm-enforce treu` silently mapped to `False`; now raises `argparse.ArgumentTypeError` with the allowed values. **Protocol contract (accept #9 as docstring clarification, rejected as behaviour change):** the existing return-value behaviour is "effective post-clamp value, regardless of write success"; the doc string said "actually applied" which contradicted both implementations. Wording corrected; behaviour unchanged. **Versioning (accept #11):** Chart.yaml bumped 1.0.0 → 1.1.0 (minor — new opt-in DCGM actuator on top of byte-identical NVML default). **Reviewer-stale rejection (#1 first half):** review claimed `current_w`/`default_w` were missing from §6.1; they were added in v1.5 (lines 382-403); reviewer used a pre-v1.5 snapshot. |
| v1.5 | 2026-05-20 | Author lock-ins after second external review pass on DCGM source. Six corrections covering correctness, doc fidelity to source, cross-library identity, and Protocol surface area: **(5) Migrate `_restore_orphaned_gpus_on_startup` onto the actuator + preserve the `current_w < default_w` guard.** Pre-v1.5 it used inline NVML, which on the DCGM path would bypass `nvidia-dcgm` (desyncing the hostengine's target-config record from the driver-level cap on every orphan-restore). Now it takes an `Actuator` argument and dispatches all six operations through the Protocol. The Protocol gains `current_w(gpu_idx) -> int` and `default_w(gpu_idx) -> int` expressly so the `current<default` guard survives the migration — without these the migrated function would issue a redundant privileged write on every startup for every previously-managed GPU. `DcgmActuator.current_w` reads field 160 (`DCGM_FI_DEV_POWER_MGMT_LIMIT`); `default_w` reads field 163 (DEF), and `restore_default` is now a one-liner that calls `apply_cap(idx, default_w(idx))`. See §6.1 Protocol additions, `test_orphan_recovery.py`, and `actuator.py` for the details. **(4) Cross-library identity mapping is now UUID-keyed.** v1.x used the same `gpu_idx` to index `self._discovered_gpu_ids[gpu_idx]` (DCGM gpuId space) and to call `nvmlDeviceGetHandleByIndex(gpu_idx)` (NVML index space), silently assuming the two spaces are identical. They aren't: DCGM preserves gpuId across detach/attach by UUID match (`DcgmCacheManager.cpp:1230-1296`); MIG-mode enumeration can split the surfaces; `NVIDIA_VISIBLE_DEVICES` differences between the Power Agent pod and the `nvidia-dcgm` pod can change either side independently. `DcgmActuator` now builds a UUID-keyed map on first `list_running_pids` call (lazy so the cap-only path doesn't pay for it), invalidates it inside `_with_reconnect`, and raises loud on cross-library UUID mismatch instead of silently mis-routing. See §6.3 note 5. **(2) Strike the "~1 second re-assertion tick" claim.** v1–v1.4 framed `dcgmConfigEnforce` as creating an "internal 1-second config-watch loop" on the hostengine that re-asserts the cap continuously. A read of `DCGM/modules/config/DcgmConfigManager.cpp` shows no such loop exists. `dcgmConfigEnforce` is a manual one-shot — its only callers are the `Set` path, the explicit `EnforceConfigGpu/Group` entry-points, and `AttachGpus` (reset/reinit recovery). `dcgm_test_apis.h:180-183` is explicit: "automatically enforced after a GPU reset or reinitialization is completed" — that's the *only* automatic enforcement. So the doc's "DCGM repairs external `nvidia-smi -pl` clobbering within ~1 s" claim is fiction; both NVML and DCGM repair external clobbering only on the Power Agent's next 15-s reconcile. **(3) Strike the "cap survives Power Agent restart" claim.** Independently of the tick claim, the agent's SIGTERM handler explicitly calls `restore_default` on every managed GPU (`power_agent.py:281-298`) — a normal DaemonSet rolling restart wipes the cap intent regardless of `enforce`. §4.1 Goal 3, §7 table, and §13 summary previously asserted survival; that contradicted the agent's own shutdown path. §7 is rewritten around the *actually* DCGM-only properties: automatic re-enforce after GPU reset/reinit (citable from `DcgmConfigManager.h:113-117` + `DcgmConfigManager.cpp:664`), single-writer routing through `nvidia-dcgm`, audit-log emission, API consistency. **(1) Correctness fix in §5.5 / §6.3.** Add the `mWorkloadPowerProfiles` blanking loop. DCGM's config manager treats an all-zero workload-profile array as `ACTION_CLEAR` per `DcgmConfigManagerTests.cpp:207-231`; `c_dcgmDeviceConfig_v2()` is ctypes-zero-initialized, so without the blanking loop every cap write silently wipes the customer's profile mask. Production `actuator.py:apply_cap` patched to match. |
| v1.4 | 2026-05-19 | Author lock-ins after external review pass. **Three accepts + one rejection:** (A) **Accept** — broaden §10.3 two-writer warning to be library-agnostic (the failure mode is "any second power-cap writer on the node," not specifically NVML-vs-DCGM). (C) **Accept** — add stale-handle recovery to `DcgmActuator` per `DCGM_ST_CONNECTION_NOT_VALID` semantics demonstrated in `DCGM/testing/python3/tests/test_connection.py:48-87`; `apply_cap`/`restore_default`/`get_uuid`/`constraints_w` wrap their calls in a single-retry pattern that flushes `self._groups`, rebuilds the handle, and retries once. New §6.3.1 documents the pattern. (D) **Accept with stronger correction than the reviewer asked for** — v1.3 Q5's `mPowerLimit.val = max_w` was actually a *latent bug*, not just an undocumented assumption. The existing NVML restore (`power_agent.py:275-276`) reads `nvmlDeviceGetPowerManagementDefaultLimit` (the factory default), not the max settable limit; DCGM exposes the right concept as `DCGM_FI_DEV_POWER_MGMT_LIMIT_DEF = 163` (`dcgm_fields.py:237-238`), distinct from `_MAX = 162`. v1.4 switches the DCGM restore path to read field 163, matching NVML byte-for-byte even on hypothetical future SKUs where default < max. (B) **Reject** — the reviewer's "15-second window where the cap reverts to SKU maximum while nvidia-dcgm is restarting" is factually incorrect; the driver persists the cap across hostengine restarts (`dcgmConfigSet` writes the limit via `nvmlDeviceSetPowerManagementLimit` per `NvmlTaskRunnerGenerated.cpp:3513-3517`; what dies with the hostengine is the *in-memory Enforce-loop tracking*, not the driver-level cap value). §7 attribution row corrected — the prior "No in both modes" wording was sloppy and conflated "cap on the GPU" with "hostengine's record of the cap." |
| v1.2 | 2026-05-19 | Author lock-ins after **investigating the DCGM PID API source** (`dcgm_field_helpers.py:67-71`, `test_dcgm_reader.py:140-150`, `dcgmlib.linux_def`). Two reversals: **(1)** `DCGM_FI_DEV_COMPUTE_PIDS` is a **time-series field, not a snapshot blob** — each `c_dcgmFieldValue_v1` decodes to one `c_dcgmRunningProcess_t`, and "current PIDs on this GPU right now" is genuinely not in DCGM's public API surface (no `dcgmGetDeviceProcesses` or `dcgmGetRunningProcesses` exported from `libdcgm.so`). So v1.1's `list_running_pids` reverts to `pynvml.nvmlDeviceGetComputeRunningProcesses` even on the DCGM path. The reframing (per §6.3 note 2 below): this asymmetry is **a property of DCGM's API surface**, not a design choice — DCGM was built for time-series GPU monitoring; "snapshot of running PIDs" is an NVML-shaped query, so we use NVML for that one step. **(2)** Drop `--actuator=auto` entirely. The chart now exposes a **strict binary choice** (`agent.actuator: nvml | dcgm`, no default beyond the existing PR #9682 NVML behaviour). No runtime probe. The operator declares which actuator their cluster runs based on whether their GPU Operator has `dcgm.enabled=true`; the chart renders one DaemonSet, with one actuator, locked at startup. This eliminates "DCGM mode and NVML mode running simultaneously" by construction (only one of the two ever exists in the cluster, period — see new §6.6). |

---

## 1. TL;DR

v2.4 deferred DCGM actuation on the premise that **the useful DCGM mode
(standalone-TCP) is unavailable on the default GPU-Operator install**,
because the chart ships `dcgm.enabled: false`. That premise still holds
for the *median* cluster.

The field-engineer report changes the math for a *subset* of customers:
**some sites set `dcgm.enabled=true` because they want the standalone
`nvidia-dcgm` hostengine running for their own reasons** (multi-client
profiling, longer field-cache TTLs, third-party tooling). On those
clusters the `nvidia-dcgm` DaemonSet is already deployed, already
privileged, already listening on `Service nvidia-dcgm` (port 5555) in
the `gpu-operator` namespace. The cost-benefit table in v2.4 §1.3
("adds a privileged DS") flips: **the privileged DS is already there**.

This doc designs the Power Agent to support both paths, **declared
explicitly at chart-install time** by the operator who knows their
cluster's DCGM posture:

1. **NVML actuator** — the path that shipped in PR #9682. Used on
   clusters where the GPU Operator's `dcgm.enabled=false` (default).
   Unchanged behaviour. Helm: `agent.actuator: nvml` (the chart
   default; matches PR #9682).
2. **DCGM actuator** — used on clusters where `dcgm.enabled=true` in
   the GPU Operator. Connects standalone-TCP to the operator-managed
   `nvidia-dcgm` hostengine, calls `dcgmConfigSet(mPowerLimit.val=W)`,
   optionally calls `dcgmConfigEnforce` to register the cap as the
   hostengine's "target configuration" so it gets re-applied
   automatically after a GPU reset or reinit
   (`DcgmConfigManager.h:113-117`, `dcgm_test_apis.h:180-183`). This
   is a narrower property than v1.x of this doc claimed — see §7 and
   the v1.5 changelog. Helm: `agent.actuator: dcgm`.

The selection rule is binary and declared by the operator:

- **DCGM enabled in deployment → operator sets `actuator: dcgm`.**
- **DCGM disabled in deployment → operator leaves `actuator: nvml` (the default).**

There is **no runtime probe and no auto-detection** (v1.2 — v1.1's
`auto` mode was dropped after investigating the DCGM PID API; see
revision history). The chart renders **one DaemonSet, with one
actuator, locked at startup**, and the chart fails template-time if
both modes were somehow requested. Mutual exclusion is enforced by
chart construction (§6.6), not by hopeful runtime behaviour.

The agent's cgroup parser, multi-pod policy, persistent
`managed_gpus.json` format, and Prometheus metric names are
**observably unchanged** — they live above an `Actuator` abstraction
that today's NVML calls satisfy trivially. The reconciler, SIGTERM
handler, and orphan recovery now dispatch through the actuator
surface so `agent.actuator: dcgm` actually means something at runtime
(v1.6 wiring); their externally-observable behaviour on the NVML
path is byte-equivalent to PR #9682, but the code path is via
`self._actuator.{list_running_pids,apply_cap,restore_default,shutdown}`
rather than inline `pynvml` calls. See §6.2 + §10.5 for the
NVML-path equivalence proof.

**What we are not doing:**
- Not flipping `dcgm.enabled=true` in dynamo's `setup-monitoring.sh`.
  That stays the customer's choice. v2.4's defer applies in full to
  sites that don't opt in.
- Not embedding `nv-hostengine` inside the Power Agent pod (v2.4 §4
  "embedded mode" — buys nothing on resilience axis, costs +400 MB
  image or new vendoring pipeline).
- Not bundling a `nv-hostengine` sidecar (v2.4 §4 "sidecar mode" —
  same).
- Not changing the `dynamo.nvidia.com/gpu-power-limit` annotation
  contract. Both actuators consume the same integer-watts value.

---

## 2. New evidence (the v2.4 §8 re-open trigger)

v2.4 §8's literal re-open language was:

> **Re-open trigger:** if dynamo's cluster-bring-up script flips
> `dcgm.enabled=true` (because the platform team has a separate reason
> to want a standalone hostengine), §1.2 Statement 2's table flips:
> the standalone-TCP DCGM mode becomes available and §6's design
> decisions become live work.

The trigger as written presumed **dynamo** would flip the flag. The
field-engineer evidence is that **customers** flip it for their own
reasons (DCGM-Exporter performance, multi-client metric scraping,
NVIDIA AI Enterprise default postures). The implication for the Power
Agent is identical: on those clusters the standalone-TCP hostengine is
reachable, so the only DCGM mode that delivers a property NVML cannot
provide is now reachable too.

v2.4 §8 also added a second condition before the re-open:

> the re-open should explicitly justify why the new property
> (`Enforce()` persistence across agent restarts) is worth the extra
> privileged surface, *and* should plan to drop the Power Agent's
> own `privileged: true` requirement by removing the hostPath state
> file and the `/proc/{pid}/cgroup` parsing dependency.

Both conditions are addressed in §8 of this doc. Short version:
**the privileged-surface delta is zero on the customer cluster**
(the customer already runs `nvidia-dcgm`), and the Power Agent's own
`privileged: true` requirement is **unchanged** — that's a real cost
the doc accepts rather than hides.

---

## 3. Which v2.4 statement does the new evidence flip?

Mapping the new evidence onto v2.4's three load-bearing statements:

| v2.4 Statement | Was | Now | Status |
|---|---|---|---|
| **§1.2 S1** — GPU Operator is NVIDIA's general installer; Dynamo just consumes it | True | True | Unchanged. Dynamo still does not ship GPU Operator opinions; it consumes whatever the customer installs. |
| **§1.2 S2** — `dcgm.enabled` defaults OFF; standalone hostengine unavailable by default | True universally | **True on default, false on customer-opt-in subset** | **Partially flipped.** The set of clusters where standalone-TCP works is no longer empty. |
| **§1.2 S3** — DCGM internally calls NVML; hardware action is identical | True | True (with **even stronger in-source proof** — see §5.2) | Unchanged. The DCGM call still bottoms out at `nvmlDeviceSetPowerManagementLimit`. |
| **§1.3** — Privilege budget worsens (1 privileged DS → 2) | True for default clusters | **False for customer subset** — on opt-in clusters the customer already runs the second privileged DS regardless of our choice | Flipped on the customer subset. |

S3 unchanged is critical: **the hardware action is byte-equivalent
across the two actuators.** No GPU is set to a different wattage by
choosing one over the other; what changes is *who orchestrates the
write*, *whether the cap is registered as DCGM's "target
configuration" so it gets re-applied automatically after GPU
reset/reinit*, and *whether power-cap writes flow through the same
hostengine the rest of the customer's DCGM tooling already uses*.

The flip on S2 and §1.3 is **subset-conditional**: the new evidence
doesn't claim S2 is universally false, it claims S2 has a non-empty
counterexample set. The design follows: **NVML stays the default**
(v2.4 logic holds for sites that match the default cluster shape);
**DCGM becomes an opt-in** (for sites in the counterexample set).

---

## 4. Goals and non-goals

### 4.1 Goals

1. **Add a DCGM actuation path that customers with `dcgm.enabled=true` can opt into.** Single Helm value (`agent.actuator: nvml | dcgm`) selects the path — strict binary, no third "auto" mode.
2. **Preserve PR #9682's behaviour unchanged when `actuator: nvml`.** Today's NVML code path becomes one of two implementations behind an `Actuator` Protocol. Byte-for-byte equivalent. No new failure modes on sites that don't opt in. `nvml` is the chart default.
3. **Make the DCGM path deliver properties NVML cannot.** With `enforce: true`, the cap becomes part of DCGM's "target configuration" and is automatically re-applied after GPU reset or reinit (`DcgmConfigManager.h:113-117`, `dcgm_test_apis.h:180-183`, `DcgmConfigManager.cpp:664` — the only automatic enforcement in the DCGM source). Even at the `enforce: false` default, the DCGM actuator routes the write through the operator-managed `nvidia-dcgm` hostengine, giving (a) a single-writer path through the customer's DCGM stack, (b) audit logging in `nvidia-dcgm`'s log surface, and (c) API consistency with the rest of the customer's DCGM tooling (`dcgmi config --get`). **What this goal explicitly does NOT claim** (corrected in v1.5; see changelog): the cap surviving Power Agent restart (the agent's SIGTERM handler restores default on every managed GPU; see `power_agent.py:281-298`) and the cap being repaired on a sub-reconcile-tick timescale against external `nvidia-smi -pl` clobbering (there is no continuous re-enforce loop in DCGM source; both NVML and DCGM repair clobbering only on the next 15-s reconcile).
4. **Enforce single-actuator-per-deployment by chart construction.** Chart renders one DaemonSet, with one actuator, locked at startup. No two pods, no two modes, no transient overlap. Mutual exclusion is a property of the chart, not a runtime assertion. (See §6.6.)
5. **Keep the agent's surface area boring.** Cgroup parser, multi-pod policy, UUID-gated state, Prometheus metric names, RBAC — all unchanged. Reconciler and SIGTERM handler are now actuator-dispatched (v1.6 wiring) but observably unchanged on the NVML path; the v1.6 changelog and the `test_reconcile_wiring.py` + `test_shutdown.py` suites document and pin the equivalence.

### 4.2 Non-goals

- **Not flipping `dcgm.enabled=true` in dynamo's bring-up script.** That stays the customer's choice. v2.4 §1.2 S2's "Dynamo never sets this flag" remains true.
- **Not switching the agent container base image to `nvcr.io/nvidia/cloud-native/dcgm`.** v2.4 §6 decision (5): vendor pydcgm `.py` files + `libdcgm.so` into the existing image (~5 MB additional), instead of taking the full DCGM base image (+400 MB). Confirmed below in §10.
- **Not exposing DCGM workload power profiles** (DCGM 4.x `dcgmConfigSetWorkloadPowerProfile`). The annotation contract stays integer watts. Workload profiles are a v2 conversation; this doc keeps the annotation byte-equivalent across actuators.
- **Not refactoring the cgroup parser, multi-pod policy, or state file.** They are actuator-agnostic. (Note: the cgroup parser still runs on the DCGM path. DCGM has no concept of K8s pod identity — `/proc/{pid}/cgroup` is the only path from PID to pod UID, regardless of which library produced the PID.)
- **Not dropping the Power Agent's `privileged: true`.** §8 explains why this is honest about §1.3 of v2.4, not in conflict with it.
- **Not auto-detecting which actuator to use at runtime.** v1.2 decision: the chart's `agent.actuator` value is the single source of truth, declared by the operator. No TCP probe, no fall-through logic, no surprises based on transient `nvidia-dcgm` reachability. The operator who set `dcgm.enabled=true` in their GPU Operator knows that fact and is responsible for setting `agent.actuator: dcgm` in the Power Agent chart values.
- **Not symmetrically routing every actuator call through the chosen library.** v1.2 reversal: when the DCGM actuator is selected, `list_running_pids` calls `pynvml.nvmlDeviceGetComputeRunningProcesses` because DCGM has no public snapshot-of-running-PIDs API (`DCGM_FI_DEV_COMPUTE_PIDS` is a time-series field; see §6.3 note 2). This is not an asymmetry of choice but of upstream API shape — DCGM is built for time-series monitoring, NVML for snapshot queries. We use the right tool per call.

---

## 5. Where the DCGM evidence holds up under in-source review

v2.4's evidence was upstream-repo grep + dcgm-exporter `go.mod`. With
a local clone of the DCGM source tree
(https://github.com/NVIDIA/DCGM), three of v2.4's claims tighten from
"documented" to "proven by source line". Paths below are repo-
relative to that DCGM checkout — drop them into any DCGM tag at or
near the version pinned by the chart (4.2.x at time of writing) to
reproduce the citations:

### 5.1 DCGM's `Set` is `dcgmConfigSet` in C — confirmed

```44:46:DCGM/testing/python3/DcgmGroup.py
            ret = dcgm_agent.dcgmConfigSet(
                self._dcgmHandle.handle, self._groupId, config, status.handle)
```

The Python `DcgmGroupConfig.Set(config)` wraps `dcgm_agent.dcgmConfigSet`. The C entry point:

```283:289:DCGM/dcgmlib/entry_point.h
DCGM_ENTRY_POINT(
    dcgmConfigSet,
    tsapiEngineConfigSet,
    (dcgmHandle_t pDcgmHandle, dcgmGpuGrp_t groupId, dcgmConfig_t *pDeviceConfig, dcgmStatus_t statusHandle),
```

### 5.2 `dcgmConfigSet` → `nvmlDeviceSetPowerManagementLimit` — proven by source line

This is the strongest evidence for v2.4 §1.2 Statement 3. The DCGM Config Manager's NVML wrapper:

```3513:3517:DCGM/dcgmlib/src/NvmlTaskRunnerGenerated.cpp
        log_debug("NvmlDeviceSetPowerManagementLimitImpl: generation mismatch {}, {}", device.generation, GetGeneration());
        return NVML_ERROR_UNINITIALIZED;
    }
    return ::nvmlDeviceSetPowerManagementLimit(device.nvmlDevice,  limit);
```

The DCGM test suite asserts the count of NVML calls behind each DCGM operation:

```1216:1218:DCGM/testing/python3/tests/test_bind_unbind_gpus.py
    set_power_limit(group, 256)
    # Since the GPU 1 is detached, nvmlDeviceSetPowerManagementLimit should have been called 1 time for GPU 2
    assert_nvml_func_call_count(handle, "nvmlDeviceSetPowerManagementLimit", 1)
```

Implication: choosing DCGM over NVML does not change the hardware
action. The two actuators are byte-equivalent at the silicon. What
differs is the orchestration layer above.

### 5.3 `dcgmConfigEnforce` — the orchestration property NVML lacks

```336:339:DCGM/dcgmlib/entry_point.h
DCGM_ENTRY_POINT(dcgmConfigEnforce,
                 tsapiEngineConfigEnforce,
                 (dcgmHandle_t pDcgmHandle, dcgmGpuGrp_t groupId, dcgmStatus_t statusHandle),
```

```236:239:DCGM/dcgmlib/dcgm_test_apis.h
 * This API provides a mechanism to the users to manually enforce the configuration at any point of
 * time. The configuration can only be enforced if it's already configured using the API \ref
 * dcgmConfigSet.
```

`dcgmConfigEnforce` re-asserts the most recent `dcgmConfigSet` value
**on demand only** — it is a one-shot manual API, not a tick-driven
loop. The only automatic re-enforcement in DCGM source is at GPU
reset/reinit time:

```113:117:DCGM/modules/config/DcgmConfigManager.h
     * Used to enforce previously set configuration for the specified GPU or group. The method is to enforce
     * device configuration such as ecc mode, power limits, clocks and compute mode.
     * Must be called after GPU reset is called in order to retain the configuration before reset.
```

```180:183:DCGM/dcgmlib/dcgm_test_apis.h
 * This API can get the most recent target or desired configuration set by \ref dcgmConfigSet.
 * Set type as \a DCGM_CONFIG_TARGET_STATE to get target configuration. The target configuration
 * properties are maintained by DCGM and are automatically enforced after a GPU reset or
 * reinitialization is completed.
```

The internal call chain confirms it. The non-`AttachGpus`,
non-explicit-API callers of `HelperEnforceConfig` are exactly: (a) the
`Set` path (one-shot after a `dcgmConfigSet`), (b) the explicit
`dcgmConfigEnforce` entry point. No timer, no cadence — searched
`modules/config/` for periodic / tick / reapply / re-enforce loops and
found none.

So the property the DCGM actuator genuinely buys with
`enforce: true` is: **DCGM remembers the cap as "target
configuration" and re-applies it for us when the hardware comes back
from a reset/reinit.** That's narrower than the v1.x doc claimed
(see v1.5 changelog) but it is real, source-grounded, and meaningful
on clusters that see driver resets — e.g. GPU recovery after XID
errors that trigger `nvmlDeviceReset`, GB200 partition rebuilds, or
field-engineer-initiated `nvidia-smi --gpu-reset`.

### 5.4 `DcgmHandle` connection modes

```29:107:DCGM/testing/python3/DcgmHandle.py
    def __init__(self,
                 handle=None,
                 ipAddress=None,
                 opMode=dcgm_structs.DCGM_OPERATION_MODE_AUTO,
                 persistAfterDisconnect=False,
                 unixSocketPath=None,
                 timeoutMs=0,
                 decoratorHandle=None
                 ):
        ...
        # If neither ipAddress nor unixSocketPath are present, start an embedded host engine
        if ipAddress is None and unixSocketPath is None:
            self.handle = dcgm_agent.dcgmStartEmbedded(opMode)
            self.isEmbedded = True
            ...

        if ipAddress is not None:
            connectToAddress = "tcp://" + ipAddress
        else:
            connectToAddress = "unix://" + unixSocketPath

        self.handle = dcgm_agent.dcgmConnect_v3(
            connectToAddress, connectParams)
```

Three connection modes, three caller incantations:

| Caller intent | Constructor args | What dynamo uses |
|---|---|---|
| Connect to `nvidia-dcgm` standalone DS in `gpu-operator` namespace | `DcgmHandle(ipAddress="nvidia-dcgm.gpu-operator.svc.cluster.local:5555")` | **Yes — this is the v3 DCGM path** |
| Connect to a unix-socket hostengine on disk | `DcgmHandle(unixSocketPath="/var/run/nvidia-dcgm/dcgm.sock")` | No — socket doesn't exist on default install (v2.4 §1.2 S2) |
| Run an in-process embedded hostengine | `DcgmHandle()` (no args) | No — embedded buys nothing on resilience axis (v2.4 §4) |

### 5.5 The C struct shape for setting a power cap

```113:124:DCGM/testing/python3/tests/test_configmanager.py
    config_values = dcgm_structs.c_dcgmDeviceConfig_v2()
    config_values.mEccMode = dcgmvalue.DCGM_INT32_BLANK
    config_values.mPerfState.syncBoost = dcgmvalue.DCGM_INT32_BLANK
    config_values.mPerfState.targetClocks.memClock = dcgmvalue.DCGM_INT32_BLANK
    config_values.mPerfState.targetClocks.smClock = dcgmvalue.DCGM_INT32_BLANK
    config_values.mPowerLimit.val = dcgmvalue.DCGM_INT32_BLANK
    config_values.mComputeMode = dcgmvalue.DCGM_INT32_BLANK
    for bitmapIndex in range(dcgm_structs.DCGM_WORKLOAD_POWER_PROFILE_ARRAY_SIZE):
        config_values.mWorkloadPowerProfiles[bitmapIndex] = dcgmvalue.DCGM_INT32_BLANK
```

The `mWorkloadPowerProfiles` blanking loop is **load-bearing**: DCGM's
config manager treats an all-zero workload-profile array as
`DCGM_CM_WORKLOAD_POWER_PROFILE_ACTION_CLEAR`
(`DcgmConfigManagerTests.cpp:207-231`), while
`DCGM_INT32_BLANK = 0x7FFFFFF0` means "no-op." Since
`c_dcgmDeviceConfig_v2()` ctypes-initializes to zero, omitting the
loop would silently clear any workload profiles the customer or
another tool had configured on every dynamo cap write. Every
upstream pattern blanks the array via this loop —
`test_configmanager.py:123-124`, `test_bind_unbind_gpus.py:1109-1110`,
`test_workload_power_profiles.py`, `test_utils.py:2885-2886`.

To set a per-GPU cap:

```265:268:DCGM/testing/python3/tests/test_configmanager.py
    config_values.mPowerLimit.type = dcgm_structs.DCGM_CONFIG_POWER_BUDGET_GROUP
    config_values.mPowerLimit.val = powerLimit * \
        len(gpuIds)  # Assumes homogenous GPUs
```

For dynamo's case (per-physical-GPU enforcement, one pod per GPU under
the supported topology):

- `mPowerLimit.type = DCGM_CONFIG_POWER_CAP_INDIVIDUAL`
- `mPowerLimit.val = <watts>` (not milliwatts — DCGM's config struct is
  in watts; v2.4 §7.1 had this right)

This is the **only** field the dynamo actuator sets. Every other
config field stays `DCGM_INT32_BLANK` so DCGM doesn't reset ECC mode,
clocks, or compute mode out from under the workload.

---

## 6. Design

> **Source of truth.** The Python listings in §6.1–§6.5 are the
> design-time pseudo-code. The implementation lives in
> `components/power_agent/actuator.py`; treat that file as
> authoritative on signatures, error handling, and clamp/persistence
> ordering. The samples below are kept readable by omitting
> non-load-bearing detail (logging, lazy imports, defensive
> bookkeeping) — if a sample disagrees with the code, the code wins.
> v1.7 review #5 fixed three specific drifts (UUID-persistence
> ownership in §6.1, UUID-keyed PID routing in §6.3, `_make_actuator`
> metrics in §10.2); structural drift between sample and code is an
> accepted cost of having both.

### 6.1 The `Actuator` Protocol

A new module `components/power_agent/actuator.py` defines:

```python
from typing import Protocol


class Actuator(Protocol):
    """Power-cap actuator surface used by power_agent.PowerAgent.

    Two implementations:
      - NvmlActuator (default; PR #9682 behaviour unchanged)
      - DcgmActuator (opt-in via --actuator=dcgm; explicit, not auto-detected)

    Both produce byte-identical hardware actions. The DCGM variant
    additionally, when constructed with enforce=True, registers the
    cap as DCGM's target configuration via dcgmConfigEnforce so that
    DCGM re-applies it after a GPU reset or reinit
    (DcgmConfigManager.h:113-117). There is no tick-driven re-enforce
    loop in DCGM; v1.x prose to that effect was incorrect — see the
    v1.5 changelog and §5.3.
    """

    name: str  # "nvml" | "dcgm"

    def init(self) -> None: ...
    def shutdown(self) -> None: ...

    def device_count(self) -> int: ...
    def get_uuid(self, gpu_idx: int) -> str: ...
    def list_running_pids(self, gpu_idx: int) -> list[int]: ...

    def constraints_w(self, gpu_idx: int) -> tuple[int, int]:
        """SKU min/max in watts. Used by _clamp_to_constraints()."""

    # ----- v1.5 additions -----
    def current_w(self, gpu_idx: int) -> int:
        """Cap currently applied to the GPU (watts).

        Distinct from default_w and from constraints_w — this is
        whatever value the driver has live, set by whichever process
        last wrote it. Lifted onto the Protocol surface in v1.5 so
        that _restore_orphaned_gpus_on_startup can preserve its
        `current_w < default_w` guard while migrating off raw NVML
        onto actuator.restore_default. NVML side wraps
        nvmlDeviceGetPowerManagementLimit; DCGM side reads
        powerLimits.curPowerLimit from GetGpuAttributes (v1.10 — the
        pre-v1.10 field-cache read of DCGM_FI_DEV_POWER_MGMT_LIMIT
        returned DCGM_FP64_BLANK on a fresh hostengine).
        """

    def default_w(self, gpu_idx: int) -> int:
        """Factory-default TGP (watts).

        NVML side: nvmlDeviceGetPowerManagementDefaultLimit. DCGM
        side: powerLimits.defaultPowerLimit from GetGpuAttributes
        (v1.10 — distinct from powerLimits.maxPowerLimit; see §6.3
        note 4 and the v1.4 / v1.10 changelogs).
        """
    # ----- end v1.5 additions -----

    def apply_cap(self, gpu_idx: int, watts: int) -> int:
        """Apply cap. Return the effective post-clamp value (v1.6 clarification).

        Return-value contract: the int returned is
        `max(min_w, min(watts, max_w))` against the SKU constraints,
        regardless of whether the underlying write succeeded. Failures
        are reported via `metrics.apply_failures_total`, not via the
        return value or exceptions. v1.5 doc wording said "actually
        applied" which contradicted both implementations — corrected in
        v1.6 per review comment #9.

        Persists the managed-GPU UUID to /var/lib/dynamo-power-agent/
        on success (v1.7 clarification per review comment #5a — v1.5
        doc wording incorrectly said "caller is responsible"; both
        actuators have always owned persistence internally via
        `power_agent._record_managed_gpu_uuid` (NVML) and
        `power_agent._record_managed_gpu_by_uuid` (DCGM)). On the
        DCGM path, UUID lookup failure is non-fatal: the cap is
        recorded in the in-memory managed set and a warning is
        logged, but the persistent file isn't updated.
        """

    def restore_default(self, gpu_idx: int) -> None:
        """Restore SKU default TDP. Called on SIGTERM and orphan recovery.

        Per v1.7 review comment #4, the DCGM implementation propagates
        write failures as exceptions (rather than absorbing them like
        apply_cap does) so SIGTERM and orphan-recovery callers don't
        log false-positive "Restored GPU N" success lines when
        dcgmConfigSet silently failed. The NVML implementation
        delegates to nvmlDeviceSetPowerManagementLimit, which raises
        NVMLError on failure — same propagation contract by default.
        """
```

This Protocol is **mechanically extractable** from today's
`components/power_agent/power_agent.py`. The current module-level
functions `_clamp_to_constraints`, `_apply_cap`, `_nvml_uuid`,
`_restore_orphaned_gpus_on_startup`, and the body of `_handle_sigterm`
are precisely the methods listed above. No new logic needed for the
NVML path — only a class boundary.

### 6.2 `NvmlActuator` — extracted from PR #9682, behaviour unchanged

```python
class NvmlActuator:
    name = "nvml"

    def init(self) -> None:
        pynvml.nvmlInit()

    def shutdown(self) -> None:
        try:
            pynvml.nvmlShutdown()
        except Exception:
            pass

    def device_count(self) -> int:
        return pynvml.nvmlDeviceGetCount()

    def get_uuid(self, gpu_idx: int) -> str:
        handle = pynvml.nvmlDeviceGetHandleByIndex(gpu_idx)
        uuid = pynvml.nvmlDeviceGetUUID(handle)
        return uuid.decode("ascii") if isinstance(uuid, bytes) else uuid

    def list_running_pids(self, gpu_idx: int) -> list[int]:
        handle = pynvml.nvmlDeviceGetHandleByIndex(gpu_idx)
        return [p.pid for p in pynvml.nvmlDeviceGetComputeRunningProcesses(handle)]

    def constraints_w(self, gpu_idx: int) -> tuple[int, int]:
        handle = pynvml.nvmlDeviceGetHandleByIndex(gpu_idx)
        min_mw, max_mw = pynvml.nvmlDeviceGetPowerManagementLimitConstraints(handle)
        return min_mw // 1000, max_mw // 1000

    def apply_cap(self, gpu_idx: int, watts: int) -> int:
        handle = pynvml.nvmlDeviceGetHandleByIndex(gpu_idx)
        min_w, max_w = self.constraints_w(gpu_idx)
        effective_w = max(min_w, min(max_w, watts))
        pynvml.nvmlDeviceSetPowerManagementLimit(handle, effective_w * 1000)
        return effective_w

    def restore_default(self, gpu_idx: int) -> None:
        handle = pynvml.nvmlDeviceGetHandleByIndex(gpu_idx)
        default_mw = pynvml.nvmlDeviceGetPowerManagementDefaultLimit(handle)
        pynvml.nvmlDeviceSetPowerManagementLimit(handle, default_mw)
```

Mapping to today's code:
- `init` / `shutdown` ← `power_agent.py:417` (`pynvml.nvmlInit()`) and `power_agent.py:283` (`pynvml.nvmlShutdown()` inside `_handle_sigterm`)
- `device_count` ← `power_agent.py:418`
- `get_uuid` ← `_nvml_uuid()` at `power_agent.py:123-134`
- `list_running_pids` ← inline `nvmlDeviceGetComputeRunningProcesses` call at `power_agent.py:478`
- `constraints_w` + `apply_cap` ← `_clamp_to_constraints` + `_apply_cap` at `power_agent.py:208-258`
- `restore_default` ← inline block in `_handle_sigterm` at `power_agent.py:272-279`

Net effect on PR #9682's behaviour: zero. The same NVML calls happen
in the same order with the same arguments.

### 6.3 `DcgmActuator` — new, opt-in

```python
class DcgmActuator:
    """Connects standalone-TCP to the operator-managed nvidia-dcgm DS.

    Connect target defaults to nvidia-dcgm.gpu-operator.svc.cluster.local:5555,
    matching the upstream operator's Service definition. Configurable via
    --dcgm-host CLI flag for sites that install nvidia-dcgm in a non-default
    namespace.
    """

    name = "dcgm"

    def __init__(self, host: str, port: int = 5555, enforce: bool = False):
        # enforce defaults to False to match NVML's set-and-forget
        # semantics — see §11 Q2 resolution. Operators who specifically
        # want re-assertion opt in by passing enforce=True (CLI
        # --dcgm-enforce or chart agent.dcgm.enforce: true).
        self._host = host
        self._port = port
        self._enforce = enforce
        self._handle = None
        self._system = None
        # One DcgmGroup per GPU. Group-per-GPU rather than one all-GPU
        # group because per-pod policy may set different caps on
        # different physical GPUs in the same reconcile cycle, and
        # mPowerLimit is a per-group property.
        self._groups: dict[int, "pydcgm.DcgmGroup"] = {}

    def init(self) -> None:
        import pydcgm
        import dcgm_structs
        self._handle = pydcgm.DcgmHandle(
            ipAddress=f"{self._host}:{self._port}",
            opMode=dcgm_structs.DCGM_OPERATION_MODE_AUTO,
            persistAfterDisconnect=False,
            timeoutMs=5000,
        )
        self._system = self._handle.GetSystem()
        # GPU enumeration uses the hostengine's view, which is the
        # source of truth on this cluster (matches what dcgm-exporter
        # sees, what dcgmi reports, etc.).
        self._discovered_gpu_ids = sorted(self._system.discovery.GetAllGpuIds())
        # NVML is also initialized so list_running_pids() can call
        # nvmlDeviceGetComputeRunningProcesses (see note 2 below for
        # why the snapshot-of-running-PIDs read goes through NVML even
        # on the DCGM path). pynvml is already a transitive dependency
        # of the agent's existing code, so this adds no new image cost.
        import pynvml
        pynvml.nvmlInit()

    def shutdown(self) -> None:
        if self._handle is not None:
            for grp in self._groups.values():
                try:
                    grp.Delete()
                except Exception:
                    pass
            self._handle.Shutdown()
            self._handle = None
        try:
            import pynvml
            pynvml.nvmlShutdown()
        except Exception:
            pass

    def device_count(self) -> int:
        return len(self._discovered_gpu_ids)

    def get_uuid(self, gpu_idx: int) -> str:
        # v1.10: route through the synchronous device-info API rather
        # than the field cache. dcgmEntityGetLatestValues + DCGM_FI_DEV_UUID
        # returns DCGM_STR_BLANK ("<<<NULL>>>") on a fresh hostengine
        # with no companion watcher — confirmed against nv-hostengine
        # 4.5.3 in our 8xA100 e2e parity run. GetGpuAttributes wraps
        # dcgmGetDeviceAttributes (synchronous), returning a
        # c_dcgmDeviceAttributes_v3 whose .identifiers.uuid carries
        # the real UUID populated from the hostengine's discovery
        # state. Same struct also carries powerLimits — see
        # _power_limits below.
        gpu_id = self._discovered_gpu_ids[gpu_idx]
        attrs = self._system.discovery.GetGpuAttributes(gpu_id)
        return attrs.identifiers.uuid.decode("ascii") if isinstance(
            attrs.identifiers.uuid, bytes
        ) else str(attrs.identifiers.uuid)

    def list_running_pids(self, gpu_idx: int) -> list[int]:
        """Snapshot of compute PIDs on GPU — via NVML, even on the DCGM path.

        DCGM has no public C/Python API for "snapshot of currently-
        running PIDs on this GPU." `DCGM_FI_DEV_COMPUTE_PIDS` is a
        time-series field — each c_dcgmFieldValue_v1 decodes to ONE
        c_dcgmRunningProcess_t (the most-recently-sampled process; see
        DCGM/testing/python3/dcgm_field_helpers.py:67-71). Enumerating
        currently-alive PIDs through DCGM requires walking the time
        series via GetAllValuesSinceLastCall and applying a heuristic
        ("seen in last 2× watch period = alive"), which is inherently
        racy.

        NVML's nvmlDeviceGetComputeRunningProcesses, by contrast, IS a
        snapshot API — it returns the current process list from the
        driver synchronously. That's the right API shape for our
        per-reconcile reconciler.

        This is a deliberate, principled asymmetry: DCGM is the right
        tool for cap *write* (group/enforce orchestration on a
        long-lived hostengine), NVML is the right tool for snapshot
        *read*. The choice tracks the upstream API shapes, not our
        preference. See §6.3 note 2, §6.3 note 5, and §4.2 (final
        non-goal) for the full reasoning.

        UUID-keyed routing (v1.5 fix per review comment #4 / §6.3
        note 5): `gpu_idx` is the DCGM-ordered index used throughout
        the reconcile loop, NOT an NVML index. We translate via
        the lazily-built `_ensure_identity_map` (DCGM gpuId → UUID →
        NVML index) because the two libraries live in separate
        identity spaces. The v1.4-and-earlier sample shown raw
        `nvmlDeviceGetHandleByIndex(gpu_idx)` — that worked only when
        DCGM and NVML enumeration agreed by chance.
        """
        import pynvml
        self._ensure_identity_map()  # lazy: first call only
        uuid = self._dcgm_uuid_by_idx[gpu_idx]
        nvml_idx = self._nvml_index_by_uuid[uuid]
        handle = pynvml.nvmlDeviceGetHandleByIndex(nvml_idx)
        return [p.pid for p in pynvml.nvmlDeviceGetComputeRunningProcesses(handle)]

    def _power_limits(self, gpu_idx: int):
        # v1.10: single source of truth for current_w / default_w /
        # constraints_w. GetGpuAttributes returns a c_dcgmDevicePowerLimits_v1
        # struct with curPowerLimit, defaultPowerLimit, enforcedPowerLimit,
        # minPowerLimit, maxPowerLimit — all integer watts, populated
        # synchronously from the hostengine's discovery state. Replaces
        # the four pre-v1.10 dcgmEntityGetLatestValues calls against
        # DCGM_FI_DEV_POWER_MGMT_LIMIT{,_MIN,_MAX,_DEF} (fields 160-163),
        # all of which returned DCGM_FP64_BLANK (= 140737488355328.0 = 2^47)
        # on a fresh hostengine in our e2e parity run.
        gpu_id = self._discovered_gpu_ids[gpu_idx]
        return self._system.discovery.GetGpuAttributes(gpu_id).powerLimits

    def constraints_w(self, gpu_idx: int) -> tuple[int, int]:
        pl = self._power_limits(gpu_idx)
        return int(pl.minPowerLimit), int(pl.maxPowerLimit)

    def apply_cap(self, gpu_idx: int, watts: int) -> int:
        """Public Protocol entry. Clamps then delegates to _apply_cap_inner,
        absorbing write failures into apply_failures_total + returning
        the post-clamp value (per Protocol contract, v1.6 review #9)."""
        min_w, max_w = self.constraints_w(gpu_idx)
        effective_w = max(min_w, min(max_w, watts))
        try:
            return self._apply_cap_inner(gpu_idx, effective_w)
        except Exception:
            self._metrics.apply_failures_total.inc()
            return effective_w

    def _apply_cap_inner(self, gpu_idx: int, effective_w: int) -> int:
        """Set → record managed state → optional Enforce (soft failure).

        v1.8 review #2 split Set bookkeeping from Enforce failure
        handling. Pre-v1.8 the bookkeeping ran only AFTER both calls
        succeeded, so a Set-OK-Enforce-fail partial success left a
        custom cap LIVE on the GPU (dcgmConfigSet invokes
        nvmlDeviceSetPowerManagementLimit synchronously) but
        _managed_gpu_indices empty — SIGTERM wouldn't restore it,
        orphan recovery wouldn't find it.
        """
        import pydcgm
        import dcgm_structs
        # v1.10: DCGM_INT32_BLANK lives in dcgmvalue, NOT dcgm_structs
        # — verified against /shared/pydcgm/dcgmvalue.py:17 in the
        # DCGM 4.5.3 apt-installed bindings. Pre-v1.10 the import path
        # raised AttributeError on the first cap write in our e2e run.
        import dcgmvalue

        gpu_id = self._discovered_gpu_ids[gpu_idx]

        def _write_set() -> int:
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
            # Blank everything DCGM would otherwise reset out from
            # under us — see v1.5 review #1 for the
            # mWorkloadPowerProfiles ACTION_CLEAR rationale.
            cfg.mEccMode = dcgmvalue.DCGM_INT32_BLANK
            cfg.mComputeMode = dcgmvalue.DCGM_INT32_BLANK
            cfg.mPerfState.syncBoost = dcgmvalue.DCGM_INT32_BLANK
            cfg.mPerfState.targetClocks.smClock = dcgmvalue.DCGM_INT32_BLANK
            cfg.mPerfState.targetClocks.memClock = dcgmvalue.DCGM_INT32_BLANK
            for i in range(dcgm_structs.DCGM_WORKLOAD_POWER_PROFILE_ARRAY_SIZE):
                cfg.mWorkloadPowerProfiles[i] = dcgmvalue.DCGM_INT32_BLANK
            cfg.mPowerLimit.type = dcgm_structs.DCGM_CONFIG_POWER_CAP_INDIVIDUAL
            cfg.mPowerLimit.val = effective_w

            grp.config.Set(cfg)
            return effective_w

        # Set is the load-bearing call. Failure here means the cap is
        # NOT live — let _with_reconnect's retry (CONNECTION_NOT_VALID)
        # or re-raise (anything else) propagate to apply_cap (absorb)
        # or restore_default (surface to SIGTERM).
        result = self._with_reconnect(_write_set)

        # Set succeeded → cap is LIVE on the GPU. Record managed state
        # BEFORE attempting Enforce so a subsequent Enforce failure
        # doesn't drop tracking. v1.8 review #2.
        self._record_managed_state(gpu_idx, result)

        # Optional: register the cap as DCGM's "target configuration"
        # so the hostengine auto-reapplies after GPU reset/reinit. A
        # failure here does NOT mean the cap is gone — Set already
        # made it live. Surface via a dedicated metric to keep
        # apply_failures_total an unambiguous "cap NOT live" signal.
        if self._enforce:
            def _write_enforce() -> None:
                grp = self._groups.get(gpu_idx)
                if grp is None:
                    # _with_reconnect recovery cleared the cache
                    # between Set and Enforce. Next reconcile will
                    # re-issue both naturally.
                    raise RuntimeError(
                        f"group cache for GPU {gpu_idx} was cleared "
                        "between Set and Enforce; recovery in flight"
                    )
                grp.config.Enforce()

            try:
                self._with_reconnect(_write_enforce)
            except Exception as e:
                logger.warning(
                    "dcgmConfigEnforce failed for GPU %d after successful "
                    "Set (cap is live and tracked; only auto-reapply-after-"
                    "reset target-config registration is missing): %s",
                    gpu_idx, e,
                )
                self._metrics.dcgm_enforce_failures_total.inc()

        return result

    def _record_managed_state(self, gpu_idx: int, watts: int) -> None:
        """Post-Set bookkeeping shared by Set-success and Set-OK-Enforce-failed."""
        import power_agent
        power_agent._managed_gpu_indices.add(gpu_idx)
        try:
            power_agent._record_managed_gpu_by_uuid(self.get_uuid(gpu_idx))
        except Exception as e:
            logger.warning(
                "DCGM apply succeeded on GPU %d but UUID persistence failed: %s",
                gpu_idx, e,
            )
        self._metrics.applied_limit_watts.labels(gpu=str(gpu_idx)).set(watts)

    def restore_default(self, gpu_idx: int) -> None:
        """Restore factory-default TGP via powerLimits.defaultPowerLimit.

        Why this and not constraints_w()[1]: the DCGM device-attributes
        struct exposes four distinct power-limit fields on
        c_dcgmDevicePowerLimits_v1:
            curPowerLimit       current cap
            minPowerLimit       min settable
            maxPowerLimit       max settable
            defaultPowerLimit   factory default
            enforcedPowerLimit  effective post-arbitration

        defaultPowerLimit is the byte-for-byte equivalent of NVML's
        nvmlDeviceGetPowerManagementDefaultLimit, which is what the
        existing NVML restore path uses (power_agent.py:275-276 in the
        SIGTERM handler, and power_agent.py:312-314 in the orphan
        recovery on startup). On every shipped data-center SKU,
        default == max, so the two attributes return the same value —
        but that equality is a property of current SKUs, not a
        guarantee of the API. Reading the right attribute keeps the
        DCGM restore byte-for-byte equivalent to the NVML restore on
        hypothetical future SKUs where default < max (consumer-style
        cards with boost headroom).

        v1.10: this came up in the e2e parity run too — the pre-v1.10
        field-cache path returned DCGM_FP64_BLANK (2^47) for DEF=163,
        and the cap write would have blown past the SKU's true max.
        Routing through _power_limits closes that hole.
        """
        default_w = int(self._power_limits(gpu_idx).defaultPowerLimit)
        # v1.7 review #4: route through _apply_cap_inner (not the
        # public apply_cap) so DCGM write failures propagate to
        # SIGTERM and orphan-recovery callers instead of being
        # silently absorbed into apply_failures_total.
        self._apply_cap_inner(gpu_idx, default_w)
```

Five notes on this implementation:

- **One `DcgmGroup` per GPU (v1.2 simplification).** Each per-GPU
  group exists only for per-GPU config Set/Enforce — required because
  dynamo's multi-pod-per-GPU policy (`_resolve_cap_for_gpu`) can
  produce different caps on different physical GPUs in the same
  reconcile tick. A single all-GPU group for config would require
  re-issuing `Set` per cap difference, defeating the abstraction.
  (v1.1's separate `_watch_group` for PID watching is gone in v1.2
  because PID enumeration moved off DCGM — see note 2.)
- **PID enumeration uses NVML even on the DCGM path — by upstream API
  shape, not by choice (v1.2).** DCGM has no public snapshot-of-running-
  PIDs API. `DCGM_FI_DEV_COMPUTE_PIDS` is a time-series field where
  each value decodes to one `c_dcgmRunningProcess_t` (the
  most-recently-sampled process per `dcgm_field_helpers.py:67-71`),
  and the upstream test (`test_dcgm_reader.py:140-150`) demonstrates
  the read pattern is "loop calling `GetLatestGpuValuesAsFieldIdDict`
  until you see the PID you're looking for." That's wrong for our
  reconciler. `nvmlDeviceGetComputeRunningProcesses` returns the
  current snapshot synchronously from the driver, which is what we
  need. **The asymmetry is forced by DCGM's design (time-series GPU
  monitoring) vs NVML's design (snapshot device queries); we use the
  right tool per call.** A reviewer who asks "why NVML for the read
  if the actuator is DCGM" gets a one-line answer with a source
  citation, not a hedge. No `libdcgm.so` PID API exists that would
  satisfy the question differently.
- **PID-to-pod mapping is still cgroup-based, regardless of actuator.**
  NVML provides the GPU→PID map (snapshot, whichever actuator is
  selected); the agent's existing `_extract_pod_uid_from_cgroup`
  provides the PID→pod-UID map. DCGM has no concept of K8s pod
  identity. The cgroup parser runs on both actuator paths unchanged.
- **`restore_default` reads field 163 (`_DEF`), not field 162 (`_MAX`).**
  See the `restore_default` docstring above — DCGM exposes a
  dedicated factory-default field that is byte-for-byte equivalent
  to NVML's `nvmlDeviceGetPowerManagementDefaultLimit`. Using `_MAX`
  would be correct on every shipped SKU today (because default ==
  max on all of them) but diverges from NVML on hypothetical future
  SKUs where default < max. Reading the right field eliminates the
  divergence by construction. (Historical note: v1.3 Q5 resolved
  this as "use `max_w`" — that was wrong, caught in v1.4 external
  review; see revision history. The four-field DCGM API surface
  (`dcgm_fields.py:232-238`) makes the right choice obvious once
  you look at it.)
- **Cross-library identity mapping is UUID-keyed, not index-keyed
  (v1.5).** DCGM gpuIds and NVML indices live in separate identity
  spaces. DCGM's cache manager preserves a gpuId across detach/attach
  by UUID match (`DcgmCacheManager.cpp:1230-1296` —
  `m_gpus[existingIndex].gpuId` is held stable while a fresh
  NVML enumeration would assign a different per-process index after
  a re-attach), and MIG enumeration can split the two surfaces
  further. Routing the NVML PID read by `gpu_idx` would therefore
  read the wrong physical GPU on any node where DCGM and NVML
  disagree on ordering. `DcgmActuator` builds a UUID-keyed map
  lazily on first `list_running_pids` call (saves init-time cost
  for cap-only callers) and invalidates it inside `_with_reconnect`
  (DCGM may re-enumerate after a hostengine restart). If a DCGM-
  visible UUID is missing from NVML's enumeration the map build
  raises a `RuntimeError` rather than silently mis-routing — that
  case usually signals a `NVIDIA_VISIBLE_DEVICES` mismatch between
  the Power Agent pod and the `nvidia-dcgm` pod, a MIG-mode
  disagreement, or a concurrent hot-plug, all of which the operator
  needs to know about. See `actuator.py:_ensure_identity_map` and
  the `test_list_running_pids_routes_via_uuid_not_gpu_idx` /
  `test_identity_map_invalidated_on_reconnect` test pair.

#### 6.3.1 Stale-handle recovery on `nvidia-dcgm` restart (v1.4)

The `DcgmActuator` holds a `DcgmHandle` (a TCP connection to
`nvidia-dcgm`) and a cache of per-GPU `DcgmGroup` objects on
`self._groups`. When `nvidia-dcgm` is evicted, killed, or upgraded
mid-run, all of these become stale: the next call into `pydcgm`
raises `dcgmExceptionClass(DCGM_ST_CONNECTION_NOT_VALID)` — the
specific error code per
`DCGM/testing/python3/tests/test_connection.py:48-87`, which
demonstrates exactly this scenario (kill hostengine via `--term`
or SIGKILL → next API call raises the connection-invalid exception).

Without recovery, the Power Agent's reconcile loop would see this
exception inside `_apply_cap`, log it as a generic apply failure
(`power_agent.py:251`), and keep retrying every 15 s with the same
stale handle — never recovering until pod restart. That's not
acceptable for an operational tool.

Recovery pattern (added in v1.4): every public method on
`DcgmActuator` that talks to the hostengine (`get_uuid`,
`constraints_w`, `apply_cap`, `restore_default`) wraps its
hostengine-touching body in a single-retry helper that:

```python
def _with_reconnect(self, op):
    """Run op(); on DCGM_ST_CONNECTION_NOT_VALID, rebuild handle +
    flush group cache + retry once. Any further failure raises.
    """
    import dcgm_structs
    try:
        return op()
    except dcgm_structs.dcgmExceptionClass(
        dcgm_structs.DCGM_ST_CONNECTION_NOT_VALID
    ):
        logger.warning(
            "nvidia-dcgm connection lost — reconnecting and "
            "flushing %d cached DcgmGroup(s).", len(self._groups),
        )
        # Drop the stale handle; do NOT try to .Delete() the cached
        # groups (their backing hostengine state is gone anyway, and
        # Delete would itself raise CONNECTION_NOT_VALID).
        self._groups.clear()
        try:
            self._handle.Shutdown()
        except Exception:
            pass
        self._handle = None
        self.init()              # re-establishes handle + GPU discovery
        return op()              # one retry; let any further error propagate
```

`apply_cap` then becomes:

```python
def apply_cap(self, gpu_idx: int, watts: int) -> int:
    return self._with_reconnect(lambda: self._apply_cap_inner(gpu_idx, watts))
```

with `_apply_cap_inner` being the body shown earlier in §6.3.

Three design choices in this pattern:

1. **Single retry, not unbounded.** A persistent
   `CONNECTION_NOT_VALID` after one reconnect attempt means
   `nvidia-dcgm` is actually gone (operator-removed, namespace
   deleted, networking broken). The right answer is to fail the
   reconcile tick and let the next 15-s reconcile try again. A
   tight retry loop here would block reconciles for other GPUs and
   would mask a sustained outage that operators need to see.
2. **Group cache is dropped, not re-`Delete()`d.** When the
   hostengine restarts, its group registry is empty; the cached
   group IDs on our side point at nothing. Calling
   `pydcgm.DcgmGroup.Delete()` on a stale group would itself raise
   `CONNECTION_NOT_VALID`. The cheapest correct action is to drop
   our local cache and let `apply_cap` create fresh groups
   lazily on next access.
3. **Reconnect from inside the call, not from a healthcheck thread.**
   A separate watchdog thread would race with the reconcile loop and
   complicate shutdown. The reconcile loop is naturally
   serialized — a 15-s tick is well above the
   "reconnect-or-fail-immediately" budget, so reconnecting inline
   is the simplest correct option.

`shutdown` is also defensive in v1.4: it suppresses
`CONNECTION_NOT_VALID` and any other `DCGMError` when calling
`self._handle.Shutdown()`, because by the time the agent itself is
shutting down, the hostengine may or may not be alive. The existing
`shutdown` already has `try/except` around `grp.Delete()`; v1.4
broadens it to be `DCGMError`-aware specifically rather than catching
bare `Exception`.

The selection rule is binary and declared at chart-install time:

| Cluster state | Operator sets | Power Agent uses |
|---|---|---|
| GPU Operator has `dcgm.enabled=true` | `agent.actuator: dcgm` | DCGM actuator (connects to `nvidia-dcgm` Service) |
| GPU Operator has `dcgm.enabled=false` (the default) | `agent.actuator: nvml` (the chart default) | NVML actuator (PR #9682 behaviour, unchanged) |

There is no runtime probe. The chart's value is the single source of
truth, declared by the operator who already knows their cluster's
DCGM posture (they're the one who set `dcgm.enabled` in the GPU
Operator chart, or accepted its default).

**Why no auto-detect (v1.2 reversal of v1.1):** v1.1 proposed a
startup TCP probe so `actuator: auto` could pick the right path
without operator action. Two reasons that's now gone:

1. **The "mutual exclusion by construction" principle (your #2).** A
   runtime probe means the agent's actuator selection is a function
   of network state at one specific instant. If the probe is flaky
   or `nvidia-dcgm` is briefly unavailable during a rollout, some
   Power Agent pods on a cluster could end up using NVML while
   others use DCGM — two writer paths, exactly what we're trying to
   prevent. Explicit declaration eliminates the possibility.

2. **The operator already knows.** Anyone deploying the Power Agent
   chart on a cluster with `dcgm.enabled=true` made that
   configuration choice themselves (or inherited it from a platform
   team that did). One Helm value (`--set agent.actuator=dcgm`)
   matches the choice they already made. The
   automation-saves-typing argument is thin and doesn't justify the
   surprise-mode-flip risk.

CLI flag on `power_agent.py main`:

```python
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
    default="nvidia-dcgm.gpu-operator.svc.cluster.local",
    help=(
        "DCGM hostengine host. Default matches the upstream GPU "
        "Operator's Service. Only consulted when --actuator=dcgm."
    ),
)
parser.add_argument(
    "--dcgm-port",
    type=int,
    default=5555,
    help="DCGM hostengine port. Default matches upstream nvidia-dcgm hostPort.",
)
parser.add_argument(
    "--dcgm-enforce",
    # v1.6 review #10: was `lambda x: x.lower() in ("true", "1", "yes")`,
    # which silently mapped `treu` → False. Now a strict allowlist via
    # `_parse_bool_strict` that raises argparse.ArgumentTypeError on
    # unknown values. The Helm chart's `validateEnforce` helper
    # (v1.7 #7) mirrors the same allowlist at template time.
    type=_parse_bool_strict,
    default=False,
    help=(
        "Call dcgmConfigEnforce after each dcgmConfigSet. Default false "
        "(set-and-forget, matches NVML's semantics). Set true to register "
        "the cap as DCGM's target configuration so the hostengine "
        "re-applies it automatically after a GPU reset or reinit "
        "(DcgmConfigManager.h:113-117). This is the only automatic "
        "re-enforcement DCGM provides — there is no tick-driven loop. "
        "Cost: one extra DCGM RPC per agent reconcile per GPU. "
        "Recommended for sites that see frequent GPU resets (XID-driven "
        "recovery, partition rebuilds, manual nvidia-smi --gpu-reset); "
        "not needed otherwise."
    ),
)
```

`PowerAgent.__init__` constructs the actuator from these args:

```python
def _make_actuator(args, metrics: PowerAgentMetrics) -> Actuator:
    """Both actuators require a metrics object; `apply_cap` raises
    RuntimeError without one (covered by test_apply_cap_requires_metrics
    in both test files). v1.6 doc sample omitted the metrics parameter
    — corrected in v1.7 per review comment #5c."""
    if args.actuator == "nvml":
        return NvmlActuator(metrics=metrics)
    if args.actuator == "dcgm":
        return DcgmActuator(
            host=args.dcgm_host,
            port=args.dcgm_port,
            enforce=args.dcgm_enforce,
            metrics=metrics,
        )
    # argparse's choices= guarantees we never reach here.
    raise ValueError(f"Unknown actuator: {args.actuator!r}")
```

Notice what's gone vs v1.1: no `_dcgm_reachable` probe, no `auto`
branch, no fall-through, no socket import. The DcgmActuator's own
`init()` will attempt the `pydcgm.DcgmHandle(ipAddress=...)`
connection and raise loudly if `nvidia-dcgm` isn't reachable — that's
the natural failure mode (CrashLoopBackOff with a clear `pydcgm`
exception), aligned with "operator declared DCGM, DCGM is mandatory."
If a customer with `dcgm.enabled=false` mistakenly sets
`actuator: dcgm`, they see the connection error on
`kubectl describe pod` and fix the config. No silent NVML fallback.

### 6.5 Helm values

```yaml
agent:
  safeDefaultWatts: 500
  prometheusPort: 9100
  # NEW in v3 (this doc; v1.2 binary choice — no `auto`):
  actuator: nvml                # nvml | dcgm
  dcgm:
    # Only consulted when actuator=dcgm. Default targets the standard
    # nvidia-dcgm Service in the GPU Operator's namespace; override
    # if your operator installs DCGM in a non-default namespace.
    host: nvidia-dcgm.gpu-operator.svc.cluster.local
    port: 5555
    # enforce=false matches NVML's set-and-forget semantics. Flip to
    # true to register the cap as DCGM's target configuration so it
    # gets re-applied after a GPU reset or reinit
    # (DcgmConfigManager.h:113-117). There is no tick-driven re-
    # enforce loop in DCGM — see §5.3, §7 row "Cap survives GPU
    # reset/reinit", and the v1.5 changelog. The off-by-default
    # rationale is in §11 Q2.
    enforce: false
```

The template renders into `command:` args verbatim — same pattern as
today's `--safe-default-watts=$(SAFE_DEFAULT_WATTS)`.

**Why `nvml` as the chart default (v1.2 — reverses v1.1).** Two reasons:

1. **Backward compatibility with PR #9682.** Operators who upgrade
   the chart without changing values get exactly the same behaviour
   they had before. The only customers who get DCGM are the ones who
   *explicitly* asked for it by setting `actuator: dcgm`.
2. **Matches the upstream default.** The GPU Operator ships with
   `dcgm.enabled: false`. Sites with `dcgm.enabled: true` are the
   opt-in minority; defaulting the Power Agent to NVML matches the
   majority shape.

**Template-level guard (referenced by §6.6).** The chart's
`_helpers.tpl` gains a `validateActuator` template that fails
template-time if `agent.actuator` is anything other than `nvml` or
`dcgm`. Modelled on the existing `validateImageTag` and
`validateMutex` guards referenced at the top of
`deploy/helm/charts/power-agent/templates/daemonset.yaml:1-2`. This
prevents an operator from accidentally typoing `agent.actuator: nvmll`
and getting a CrashLoopBackOff pod with an opaque `argparse` error.

### 6.6 Mutual exclusion guarantees — one writer per node, by construction

This section formalizes the design principle your #2 stated:
*multiple paths to change GPU config is always a bad design.* We enforce
single-writer-per-node by chart construction, not by runtime
assertions or by hopeful behaviour. Five guarantees, each verifiable
from a specific code surface:

| # | Guarantee | Where it's enforced | What would break it |
|---|---|---|---|
| 1 | The chart renders exactly one Power Agent DaemonSet, regardless of `agent.actuator` value. | `deploy/helm/charts/power-agent/templates/daemonset.yaml` is one file rendering one resource. There is no second DaemonSet template for the "DCGM variant." | Splitting the template into two `kind: DaemonSet` resources gated by `if`. We explicitly do not do this. |
| 2 | At most one Power Agent pod per node. | DaemonSet semantics + the existing pod-anti-affinity on the chart (one DaemonSet → one pod per matched node, K8s-managed). | Adding a second DaemonSet with overlapping `nodeSelector`. Guard #1 rules this out at the template level. |
| 3 | The pod selects one actuator at startup and holds it for its lifetime. | `power_agent.py main` calls `_make_actuator(args)` exactly once before the reconcile loop begins. The actuator object is stored on `self._actuator` and never re-bound. There is no fall-over branch. | A future patch that introduces "try DCGM, fall back to NVML on first failure." We explicitly reject this in §4.2 (final non-goal). |
| 4 | The chart fails template-time if `agent.actuator` is missing or not in `{nvml, dcgm}`. | `_helpers.tpl` adds `validateActuator`, mirroring the existing `validateImageTag`/`validateMutex` pattern referenced at `daemonset.yaml:1-2`. | An operator typos `actuator: nvmll` or omits the value; without the guard they'd get a CrashLoopBackOff pod with an opaque argparse error. The guard makes the failure visible during `helm install --dry-run`. |
| 5 | Switching actuators requires a chart `helm upgrade` (DaemonSet rolling restart). | There is no in-pod "reconfigure to use the other actuator" path. The actuator is locked at `_make_actuator` time; changing it means a new pod. The DaemonSet's default `updateStrategy: RollingUpdate` with `maxUnavailable: 1` (present in `values.yaml`) guarantees the old pod terminates before the new pod starts on each node. | A `kubectl edit pod` that changes the `--actuator` arg in-place. K8s allows this for static-pod fields; the agent does not honor a config reload signal, so the change has no effect until the pod restarts — at which point the chart's value is what `_make_actuator` reads. |

**What this rules out, explicitly:**

- ❌ "Two DaemonSets, one for NVML and one for DCGM, with `nodeSelector` ensuring they don't co-locate." Splits the surface area without buying anything: still one pod per node, just at the cost of double the templates.
- ❌ "Runtime probe with fall-over to NVML if DCGM goes down." Means the writer is whichever library returned a non-error first — non-deterministic, and silently masks `nvidia-dcgm` evictions. v1.2 removes this entirely (was v1.1's `auto` mode).
- ❌ "One pod with both actuators wired in and a sidecar that selects at runtime based on health checks." Two writer paths in one pod, gated by liveness probes — multiple ways to clobber GPU config from a single pod. The opposite of what your #2 asked for.

**What about `nvidia-dcgm` itself as a writer?** The operator-managed
`nvidia-dcgm` hostengine pod is a *server*, not a writer. It does not
call `dcgmConfigSet` of its own accord; it only does so when a client
(like the Power Agent) calls into it. When `actuator: dcgm`, the
Power Agent is that client, and it is the only client calling
`dcgmConfigSet` on dynamo's behalf. Any *other* DCGM client on the
cluster (`dcgm-exporter` for metrics, `dcgmi` for ad-hoc operator
debugging) is read-only against `nvidia-dcgm`'s field cache and does
not write config. So "DCGM enabled" does not equal "multiple writers";
the DCGM mode just means dynamo's *one* writer happens to route
through `nvidia-dcgm` instead of NVML directly. Single writer either
way.

**The forbidden configuration:** an operator who runs the Power Agent
with `actuator: nvml` on a cluster where some *other* customer
tooling is calling `dcgmConfigSet` via `nvidia-dcgm` for power. That
would be two writers (one via NVML, one via DCGM). The chart cannot
detect this from inside the cluster — there's no way to enumerate
"who else is writing GPU config" — so this falls on the operator's
deployment hygiene. The doc's §10 rollout section should call this
out as a customer-facing operator note. (Added in §10.3 below.)

### 6.7 RBAC

The DCGM path needs **zero additional Kubernetes RBAC**. The agent
connects to `nvidia-dcgm.gpu-operator.svc.cluster.local:5555` like any
other in-cluster client; it does not need to read `Service`
resources, because the DNS name is static (built into the upstream
operator). The agent does need its NetworkPolicy (if any) to permit
egress to the `gpu-operator` namespace on port 5555; on clusters
without a NetworkPolicy the default Allow-All applies.

The Power Agent's existing RBAC (pod listing, see
`deploy/helm/charts/power-agent/templates/role.yaml`) is unchanged.

---

## 7. Honest accounting of what DCGM buys (and doesn't)

**This section was rewritten in v1.5** after a second pass over DCGM
source. The previous table claimed two properties that don't hold:
(a) "cap survives external `nvidia-smi -pl` clobbering via a ~1-s
hostengine tick" — there is no such tick in `modules/config/`; the
only automatic enforcement is at GPU reset/reinit. (b) "cap survives
Power Agent restart with `enforce: true`" — the agent's SIGTERM
handler calls `restore_default` on every managed GPU
(`power_agent.py:281-298`), so a normal DaemonSet rolling restart
wipes the cap intent regardless of `enforce`. See the v1.5 changelog
for the full diff; the rewritten table below states only what is
demonstrably true from source.

| Property | NVML | DCGM (`enforce: false`, default) | DCGM (`enforce: true`) |
|---|---|---|---|
| Per-GPU cap write | Yes | Yes (byte-equivalent — see §5.2) | Yes (byte-equivalent — see §5.2) |
| SKU min/max clamp | Yes (`nvmlDeviceGetPowerManagementLimitConstraints`) | Yes (`GetGpuAttributes().powerLimits.{min,max}PowerLimit` — v1.10) | Yes (same) |
| Cap repaired after external `nvidia-smi -pl <higher>` between reconciles | **No** — next 15-s reconcile re-asserts | **No** — next 15-s reconcile re-asserts (DCGM has no continuous re-enforce loop; see §5.3) | **No** — same. `dcgmConfigEnforce` is one-shot at `Set` time, not a tick. The 15-s reconcile is the repair latency on both paths. |
| Cap survives Power Agent restart | **No** — SIGTERM handler restores default (`power_agent.py:281-298`); UUID-gated orphan recovery restores on next startup | **No** — same SIGTERM/orphan-recovery path runs | **No** — same. `enforce: true` does not change the agent's SIGTERM behaviour. A future design that wanted persistent caps across restart would need to skip SIGTERM restore (see v1.5 changelog "rejected option (b)"). |
| Cap survives GPU reset / reinit | **No** — driver clears the cap; agent doesn't notice until next reconcile (≤15 s) | **No** — `enforce: false` doesn't register the cap as target configuration | **Yes** — DCGM persists the cap as "target configuration" and re-applies it after GPU reset/reinit (`DcgmConfigManager.h:113-117`, `dcgm_test_apis.h:180-183`, `DcgmConfigManager.cpp:664`). **This is the only resilience property DCGM genuinely buys over NVML.** Useful on clusters with XID-driven recoveries, GB200 partition rebuilds, manual `nvidia-smi --gpu-reset`. |
| Cap survives `nvidia-dcgm` pod restart | n/a | **Yes (the value).** The driver-level cap persists because `dcgmConfigSet` writes via `nvmlDeviceSetPowerManagementLimit` (`NvmlTaskRunnerGenerated.cpp:3513-3517`) and the driver retains it. The hostengine's in-memory target-config record is rebuilt on the next reconcile when the actuator's `_with_reconnect` (§6.3.1) catches `CONNECTION_NOT_VALID` and re-issues `Set`. | **Yes (the value), partial (the target-config record).** As `enforce: false`, plus: the in-memory target-config is rebuilt on reconnect, so reset-time auto-enforcement is restored after the next reconcile. |
| GPU → PID enumeration | `nvmlDeviceGetComputeRunningProcesses` | `nvmlDeviceGetComputeRunningProcesses` (same — see §6.3 note 2: DCGM has no public snapshot-of-running-PIDs API) | Same |
| PID → pod-UID attribution | Power Agent's cgroup parser (`/proc/{pid}/cgroup`) | Same parser — DCGM has no concept of K8s pod identity | Same |
| Multi-pod-per-GPU policy resolution | Power Agent's `_resolve_cap_for_gpu` | Same — unchanged | Same — unchanged |
| Privileged container needed for write | Yes (`runAsUser: 0` + `privileged: true`) | **Still yes** — see §8 | **Still yes** — see §8 |
| Single power-cap writer through `nvidia-dcgm` (on a cluster where other tooling uses DCGM for power) | No — separate write path | **Yes** — Power Agent and any DCGM-using tooling route through the same hostengine | Same |
| Audit-log emission for every cap write | No (Prometheus metric only) | **Yes** — `nvidia-dcgm` logs every `dcgmConfigSet` | Same, plus `dcgmConfigEnforce` calls |
| `dcgmi config --get` shows the Power Agent's intent | No | **Yes** — same surface ops use for everything else DCGM | Same |

The single resilience property DCGM genuinely buys with `enforce: true`
is **automatic re-application of the cap after GPU reset/reinit**. It
is real, source-grounded (`DcgmConfigManager.cpp:664` calls
`HelperEnforceConfig` inside the reset-needed branch, and `AttachGpus`
re-pushes preserved target configs on reinit), and meaningful for
clusters that see resets. Customers on clusters that don't (most
inference clusters) get nothing from `enforce: true` they couldn't get
from `enforce: false` plus the agent's 15-s reconcile, which is why
the default is `false` (§11 Q2).

What the default DCGM path (`enforce: false`) buys on top of NVML —
even without the reset/reinit property — is narrower but real:

- **Single writer path through `nvidia-dcgm`.** On a cluster where
  any other tooling uses DCGM for power (rare but possible), the
  Power Agent's writes route through the same hostengine as those
  tools, eliminating the NVML-vs-DCGM two-writers failure mode (§6.6,
  §10.3). NVML mode on such a cluster would create that failure mode.
- **Audit trail.** `nvidia-dcgm` logs every `dcgmConfigSet`; NVML
  writes from the Power Agent are not externally observable except
  via Prometheus metrics.
- **API consistency with the rest of the customer's DCGM stack.**
  `dcgmi config --get` shows the Power Agent's intent the same way
  it shows everyone else's; operators debugging a power-cap question
  use the same tool they use for the rest of their DCGM workflow.

Properties this design **does not** buy (corrected list per v1.5):
- It does not let the agent shed `privileged: true`.
- It does not enable per-pod GPU attribution that the cgroup parser doesn't already provide.
- It does not enable cluster-wide group budgets (`DCGM_CONFIG_POWER_BUDGET_GROUP`); the policy stays per-physical-GPU.
- It does not change the annotation contract.
- It does not survive Power Agent restart on either `enforce` setting. SIGTERM restores default; orphan recovery on the next agent startup is the safety net for "agent died ungracefully," not the survival mechanism.
- It does not repair external `nvidia-smi -pl` clobbering faster than NVML does. Both paths repair on the 15-s reconcile.

---

## 8. Privilege budget — v2.4 §1.3 revisited honestly

v2.4 §1.3 argued the DCGM path increases per-node privileged-DS count
from 1 to 2. With the customer-subset framing, the math splits two
ways:

### 8.1 Default cluster (dcgm.enabled=false)

| DaemonSet | Privileged |
|---|---|
| `dcgm-exporter` (operator-default) | No |
| `dynamo-power-agent` | Yes |

→ **1 privileged DS per GPU node.** Unchanged from v2.4. NVML
actuator default keeps this property; an operator on a default
cluster never sets `agent.actuator: dcgm` because there is no
`nvidia-dcgm` Service to connect to.

### 8.2 Customer-opt-in cluster (dcgm.enabled=true)

| DaemonSet | Privileged | Already there because... |
|---|---|---|
| `dcgm-exporter` (operator-default) | No | Operator-default |
| `nvidia-dcgm` (standalone hostengine) | **Yes** | **Customer flipped `dcgm.enabled=true` for their own reasons** |
| `dynamo-power-agent` | **Yes** | PR #9682 — unchanged in this design |

→ **2 privileged DSes per GPU node.** v2.4 §1.3's worse-case scenario.

The crucial accounting move: **the customer already pays this cost,
independent of whether dynamo offers a DCGM actuator.** They flipped
the flag for DCGM-Exporter's benefit, AI Enterprise compatibility, or
some third-party tooling. Choosing the NVML actuator on this cluster
does not save them a privileged DS — `nvidia-dcgm` runs either way.

The DCGM actuator, on this subset, is therefore: "1 already-existing
privileged DS gains a new client (the Power Agent) that calls
`dcgmConfigSet` instead of `nvmlDeviceSetPowerManagementLimit`." No
new privileged surface; the existing surface gains an authorized
caller.

### 8.3 Why the Power Agent stays `privileged: true` even on the DCGM path

v2.4 §1.3 listed three reasons the Power Agent itself is privileged:

1. **NVML cap-write refusal under non-root** — dropped on the DCGM path. The cap *write* goes through `nvidia-dcgm` (TCP, no in-process NVML write). Note that NVML is still called on the DCGM path for the snapshot-of-PIDs read (`nvmlDeviceGetComputeRunningProcesses`, §6.3 note 2), but that's a read, not a privileged write — `nvmlInit()` and read calls succeed without root.
2. **hostPath state writes** at `/var/lib/dynamo-power-agent/managed_gpus.json` — **still required.** UUID-gated orphan recovery is actuator-agnostic; the state file outlives the pod and must survive container-image upgrades.
3. **`hostPID: true` + `/proc/{pid}/cgroup` read** for pod-UID extraction — **still required.** The cgroup parser is the agent's only path from "GPU process PID" → "pod UID." DCGM does not provide pod-attribution. (And by §6.3 note 2, even when the actuator is DCGM, the *PID* still comes from NVML — but the cgroup lookup that follows it is what actually needs `hostPID: true`.)

Two of three reasons stand on the DCGM path. The privileged container
stays. This is the honest answer to v2.4 §8's second re-open condition
("plan to drop the Power Agent's own `privileged: true`"): we do
*not* drop it, and we acknowledge that openly.

v2.4 §8 framed dropping `privileged: true` as a precondition for
re-open. With the new field evidence, that framing is too tight: the
DCGM path delivers its useful properties (cap re-application after
GPU reset/reinit per §7; single-writer routing through `nvidia-dcgm`;
audit-log emission; `dcgmi config` visibility) without needing the
privilege reduction to also happen. The two are independent. This
design ships the actuator swap; a future v4 may revisit
cgroup-parsing-via-CRI-API or state-via-K8s-secret to drop privileged:
true. That work is not in scope here.

---

## 9. Container image and Python packaging

v2.4 §6 decision (5) — confirmed in this v3:

- Vendor pydcgm `.py` files + `libdcgm.so` into the existing `nvcr.io/nvidia/dynamo/power-agent` image via a multi-stage build that copies from `nvcr.io/nvidia/cloud-native/dcgm`. `pynvml` and `libnvidia-ml.so` are already present in the image (PR #9682's NVML actuator depends on them); they stay because the DCGM actuator still uses NVML for the snapshot-of-PIDs read (§6.3 note 2).
- Size impact: ~5 MB (the Python bindings + the shared library), versus +400 MB for taking the full DCGM base image.
- The vendored pydcgm bindings live under `/opt/dcgm/python` and `libdcgm.so` under `/opt/dcgm/lib`. `PYTHONPATH=/opt/dcgm/python` and `LD_LIBRARY_PATH=/opt/dcgm/lib` are baked into the image's `ENV` (Dockerfile:66-71) so neither needs entry-script setup.
- Image tag bump: existing minor → next minor. Helm chart `appVersion` follows.

The DCGM actuator's `import pydcgm` is **deferred until `init()`**, not
at module-import time:

```python
def init(self) -> None:
    import pydcgm
    import dcgm_structs
    ...
```

Reason: a Power Agent running with `--actuator=nvml` should not pay
`libdcgm.so` dlopen cost (~3s) on startup. Lazy-imports give us a
single image with two actuators and zero default-path overhead.

---

## 10. Migration plan

### 10.1 Suggested PR sequence

The work decomposes naturally into two PRs against `main`, both
opening after PR #9682 and the rest of the PR9369 split have merged
(per `pr9369-split-plan.md`):

**PR A — `Actuator` refactor (no behaviour change):**

- Extract `NvmlActuator` from `power_agent.py` module-level functions.
- Introduce `actuator.py` with the `Actuator` Protocol and `NvmlActuator` class.
- `PowerAgent.__init__` takes an `Actuator` instance; the existing entry point constructs `NvmlActuator()`.
- All 43 existing tests pass unchanged. The PR ships with no new tests beyond a small set asserting `NvmlActuator` is a `runtime_checkable` Protocol implementation.
- ~250 lines moved + ~50 lines new = ~300 lines. Reviewable in <1 hour.

**PR B — `DcgmActuator` implementation (v1.2: DCGM for writes, NVML for snapshot PID read):**

- Add `DcgmActuator` class with `init`, `shutdown`, `device_count`, `get_uuid`, `list_running_pids` (NVML-backed per §6.3 note 2), `constraints_w`, `apply_cap`, `restore_default`.
- Add `--actuator` (`nvml | dcgm`, default `nvml`), `--dcgm-host`, `--dcgm-port`, `--dcgm-enforce` CLI flags.
- Add `_make_actuator(args)` — simple branch, no probe (per v1.2 §6.4).
- Add Helm values + template wiring with default `agent.actuator: nvml`. Add `validateActuator` to `_helpers.tpl`.
- Add multi-stage Dockerfile vendoring pydcgm + libdcgm.so.
- New tests:
  - `test_dcgm_actuator.py` — unit tests with `pydcgm` mocked at the import boundary; covers `apply_cap` happy path, SKU clamp, `restore_default`, `Enforce` toggle, lazy-import-not-at-module-load, `list_running_pids` delegating to NVML.
  - `test_actuator_selection.py` — unit tests for `_make_actuator(args)`: explicit `nvml`, explicit `dcgm`, invalid arg. No probe tests (no probe to test).
  - `test_helpers_validate_actuator.py` (Helm) — render-time test that `agent.actuator: nvmll` fails template-time with a clear error.
  - `test_dcgm_actuator_live.py` (integration, gated on dev pod with `nvidia-dcgm` reachable) — single test that connects, applies, reads back, restores. Module-skipped on default clusters.
- ~560 lines new + ~140 lines test = ~700 lines (v1.4 adjustment: +60 LOC for the `_with_reconnect` helper, `restore_default` reading field 163, and the `DCGM_ST_CONNECTION_NOT_VALID` recovery tests; v1.1's ~770 line estimate was for full-DCGM PID watch + ctypes decode and is unrelated to the v1.4 additions).

Test additions in v1.4:
- `test_dcgm_actuator.py::test_stale_handle_recovery` — kill the mock hostengine mid-`apply_cap`, assert `_with_reconnect` rebuilds and the second attempt succeeds; assert `self._groups` is empty after recovery.
- `test_dcgm_actuator.py::test_stale_handle_persistent_failure` — kill the hostengine and keep it dead; assert the second attempt also fails and the exception propagates (no infinite retry).
- `test_dcgm_actuator.py::test_restore_default_reads_default_power_limit` — assert `restore_default` reads `GetGpuAttributes().powerLimits.defaultPowerLimit` (not `maxPowerLimit`), and that `dcgm_agent.dcgmEntityGetLatestValues` is NOT called (regression guard from v1.10 — the pre-v1.10 field-cache path was the load-bearing bug).

Both PRs together add ~1,000 lines on top of PR #9682. PR A is mergeable
independently; PR B depends on PR A.

### 10.2 Roll-out strategy

The v1.2 binary choice with `nvml` as the default makes the upgrade
story trivial for the majority and explicit for the minority:

| Cluster type | What the operator does | What happens |
|---|---|---|
| Default install (`dcgm.enabled=false`) | Nothing (chart default is `actuator: nvml`) | NVML actuator. Byte-identical to PR #9682. |
| Customer opt-in (`dcgm.enabled=true`) — wants DCGM actuator, set-and-forget | `--set agent.actuator=dcgm` | DCGM actuator with `enforce: false` (the chart default per §6.5 / §11 Q2). Writes route through `nvidia-dcgm` via `dcgmConfigSet`; `dcgmConfigEnforce` is NOT called. Observable cap-write behaviour matches NVML. |
| Customer opt-in (`dcgm.enabled=true`) — wants DCGM actuator AND auto-reapply after GPU reset/reinit | `--set agent.actuator=dcgm --set agent.dcgm.enforce=true` | DCGM actuator with `enforce: true`. Each reconcile calls `dcgmConfigSet` followed by `dcgmConfigEnforce` so the cap becomes DCGM's "target configuration" and is automatically re-applied after GPU reset/reinit (`DcgmConfigManager.h:113-117`). Both Helm values are required — setting `actuator=dcgm` alone does NOT activate Enforce. Note: this does NOT make the cap survive Power Agent restart, and does NOT repair external `nvidia-smi -pl` clobbering faster than NVML (see §7 corrections per v1.5). |
| Customer opt-in (`dcgm.enabled=true`) — wants to keep NVML actuator | Nothing (chart default is `actuator: nvml`) | NVML actuator. ⚠ See §10.3 below for the operator note about avoiding two-writer configurations. |
| `nvidia-dcgm` in non-default namespace, using DCGM actuator | `--set agent.actuator=dcgm --set agent.dcgm.host=nvidia-dcgm.custom-ns.svc.cluster.local` | DCGM actuator targets the override host. |
| Mistake: `actuator: dcgm` on a cluster where `nvidia-dcgm` isn't deployed | (the mistake itself) | CrashLoopBackOff with `pydcgm` connection error on `kubectl describe pod`. Operator sees the error during `helm install` testing and fixes the config. No silent fallback to NVML — the operator declared DCGM, DCGM is mandatory. |
| Mistake: `actuator: nvmll` (typo) | (the mistake itself) | `helm install --dry-run` fails with `validateActuator` template error before anything is deployed. |

Two properties worth calling out:

- **Backward compatibility is total.** Any existing PR #9682 customer
  who upgrades the chart without changing values gets exactly the
  PR #9682 behaviour. No new failure modes, no new pods, no new
  privileges.
- **DCGM-enabled customers must opt in explicitly.** The "operator
  doesn't need to know this doc exists" property from v1.1 is gone in
  v1.2, by design (your #2: explicit-only). The trade-off: one Helm
  value vs. zero. The win: zero possibility of mode-flip surprises
  during rolling restarts.

### 10.3 Operator note: avoiding two-writer configurations

The chart enforces single-actuator-per-deployment for the Power
Agent (§6.6). It cannot detect *other* power-cap writers on the
node. The principle is library-agnostic: **any second power-cap
writer on a node, regardless of which library either actor uses,
will produce flapping caps and undermine the Power Agent's
guarantees.**

Concretely, the unsafe configurations are:

| Power Agent mode | Other writer on the node | Outcome |
|---|---|---|
| `actuator: nvml` | Third-party tooling calling `dcgmConfigSet`-for-power | Flapping. Both writers eventually land in `nvmlDeviceSetPowerManagementLimit` (DCGM's bottom layer, per §5.2), but they alternate, so the observable cap walks between the two intents. |
| `actuator: dcgm` | Third-party tooling calling `nvmlDeviceSetPowerManagementLimit` directly | Flapping at the Power Agent reconcile cadence on both `enforce` settings (≈15 s repair window). Note: v1.x of this row claimed `enforce: true` repaired the clobber within ~1 s via a hostengine tick; that's wrong — DCGM has no continuous re-enforce loop (see §5.3 and v1.5 changelog). `enforce: true` only changes behaviour around GPU reset/reinit, not around external clobbering. On chassis-aware clusters with strict rack-level budgets, a third-party writer that exceeds the budget during the 15-s window can still trip breakers regardless of actuator selection — the operator-hygiene constraint in this section is what protects against that, not any DCGM property. |
| `actuator: nvml` | Third-party tooling calling `nvmlDeviceSetPowerManagementLimit` directly | Flapping. Same library, no protective layer; whichever writer ran most recently wins until the next reconcile from either side. |
| `actuator: dcgm` | Third-party tooling calling `dcgmConfigSet`-for-power on the same `nvidia-dcgm` hostengine | Flapping. Two clients writing to the same hostengine's config-cache; whichever client called most recently wins. `dcgmConfigEnforce` does not protect against another client also calling `dcgmConfigSet`-then-`Enforce` — they're peers. |

The safe configurations are the negative space of the above: the
Power Agent is the *only* tool on the node that writes a power cap,
regardless of which library it uses for the write. Read-only DCGM
clients (`dcgm-exporter`, `dcgmi --get`, `nvidia-smi --query-gpu`) do
not violate this property — they're observers, not writers.

The Power Agent cannot detect or enforce single-writer property on
the node side; this is a deployment-hygiene constraint that falls on
the operator. We document it in
`components/power_agent/README.md` §"Actuator selection" with this
table so the failure-mode question gets a complete answer regardless
of which library the operator's other tooling uses.

### 10.4 Documentation updates

- `components/power_agent/README.md` — add §"Actuator selection" subsection including the §10.3 operator note about two-writer configurations.
- `deploy/helm/charts/power-agent/README.md` — add `agent.actuator`, `agent.dcgm.*` values to the table.
- `deploy/helm/charts/power-agent/templates/_helpers.tpl` — add `validateActuator` and `validateEnforce` guards (mirrors `validateImageTag`, `validateMutex`).

### 10.5 What does **not** change

- `dynamo.nvidia.com/gpu-power-limit` annotation contract (still integer watts).
- `_extract_pod_uid_from_cgroup` parser.
- Multi-pod-per-GPU policy.
- UUID-gated orphan recovery / `managed_gpus.json` format.
- Prometheus metric names.
- `setup-monitoring.sh` (we do not flip `dcgm.enabled=true`).
- `pr9369-split-plan.md` (this work is post-split, on main).

---

## 11. Open questions for reviewer

All six questions are resolved as of v1.3. Each is kept here with
its resolution and rationale so PR B reviewers can see what was
considered and why each choice was made. Q1/Q3 also retain a trace
of the path not taken (v1.1's `auto` + full-DCGM proposals), so a
reviewer who arrives with one of those proposals doesn't re-litigate
the discussion from scratch.

1. ~~**Default selection mechanic.**~~ **RESOLVED v1.2: strict binary choice, default `nvml`, no auto-probe.** v1.1 had tried `auto` as the default with a TCP probe, but that approach was reversed for two reasons: (a) per the §6.6 mutual-exclusion-by-construction principle, a runtime probe makes the actuator a function of network state at one instant, which can produce inconsistent modes across pods during rollouts; (b) the operator who set `dcgm.enabled=true` already made one configuration choice, and asking them to make a second matching choice (`--set agent.actuator=dcgm`) is not a meaningful burden. The chart's `agent.actuator` value is the single source of truth; the chart's `validateActuator` template guard catches typos; the DcgmActuator's `init()` fails loud on `nvidia-dcgm` unreachability. Default of `nvml` matches the upstream GPU Operator default (`dcgm.enabled: false`) and PR #9682's behaviour.

2. ~~**`dcgmConfigEnforce` default.**~~ **RESOLVED v1.3: default `false`** (set-and-forget, matches NVML's semantics). Reasoning: an NVML→DCGM migration that flips both the writer-path *and* the cap-persistence-semantics in one step has a wider blast radius than necessary. Defaulting to `false` means the migration changes one thing — the writer path — while the observable cap-write behaviour stays NVML-compatible. Customers who specifically want the reset/reinit auto-reapply property opt in (`enforce: true`); they're typically the customers who see frequent GPU resets (XID-driven recoveries, partition rebuilds, manual `nvidia-smi --gpu-reset`). The §7 table now distinguishes default-off vs opt-in behaviour explicitly so a reviewer can see what flipping the bit changes. **v1.5 amendment:** the v1.3 rationale above mentioned "1-second re-assertion" and "Power Agent restart survival" as the reasons to opt into `enforce: true`. Both were wrong (see v1.5 changelog and §7 rewrite). The default stays `false` for the original conservative reason — minimize the blast radius of NVML→DCGM migration — but the property that opt-in actually buys is GPU-reset/reinit auto-reapply, not the two stronger properties v1.3 cited.

3. ~~**PID enumeration via DCGM.**~~ **RESOLVED v1.2: NVML for PID enumeration even on the DCGM path.** v1.1 had tried `DCGM_FI_DEV_COMPUTE_PIDS` + ctypes decode of `c_dcgmRunningProcess_t` to deliver "full DCGM" on the DCGM path. That was reversed after investigating the DCGM source: `DCGM_FI_DEV_COMPUTE_PIDS` is a time-series field (each `c_dcgmFieldValue_v1` decodes to *one* `c_dcgmRunningProcess_t`, per `dcgm_field_helpers.py:67-71` and `test_dcgm_reader.py:140-150`), not a snapshot. There is no public `dcgmGetDeviceProcesses`/`dcgmGetRunningProcesses` API in `libdcgm.so`. So the choice is: (a) racy time-series walk via `GetAllValuesSinceLastCall`, or (b) NVML for the read. v1.2 takes (b) and reframes the asymmetry: **DCGM is a time-series monitoring library; NVML is a snapshot device-query library; we use the right tool per call, not because we prefer one but because that's what each API gives us.** A reviewer asking "why NVML for the read if the actuator is DCGM" gets a one-line answer with source citations. This also means the DcgmActuator image vendors both `pydcgm` (for write) and `pynvml` (for snapshot read), which §9 needs to reflect.

4. ~~**Group-per-GPU vs single-group.**~~ **RESOLVED v1.3: per-GPU `DcgmGroup` for config writes.** Drives the §6.3 implementation directly: one group per physical GPU, created lazily on first cap write to that GPU and cached on `self._groups[gpu_idx]`. Reason: dynamo's multi-pod-per-GPU policy (`_resolve_cap_for_gpu`) routinely produces different caps on different GPUs in the same reconcile tick — a single all-GPU group would force re-issuing `Set` with all-but-one field blanked per difference, which both defeats the abstraction and introduces ambiguity about whether the blanked fields revert. Per-GPU groups make each `Set` self-describing for the GPU it targets. The hostengine cost is a per-GPU group object (a few KB each) — negligible vs. the cost we avoided.

5. ~~**`restore_default` semantics.**~~ **RESOLVED v1.4: read `DCGM_FI_DEV_POWER_MGMT_LIMIT_DEF` (field 163), set `mPowerLimit.val = default_w`.** (v1.3 had resolved this as "use max_w from `_MAX` (field 162)" — that was wrong, caught in external review. The existing NVML path uses `nvmlDeviceGetPowerManagementDefaultLimit` at `power_agent.py:275-276`, which returns the *factory default*, not the *max settable limit*. On every shipped data-center SKU these are the same value, so v1.3's choice would have been observably correct today — but it's not the same *concept*, and would diverge on hypothetical future SKUs where default < max. DCGM exposes both as separate fields, per `dcgm_fields.py:232-238`, so the right answer is just "use the field that matches NVML's `default` semantics." Implementation in §6.3 `restore_default`. Closes the v1.3 latent bug.) The alternative — `mPowerLimit.val = DCGM_INT32_BLANK` to clear the target — relies on hostengine-internal default-on-clear semantics that aren't documented in the public API and would still leave open the question of which field to read; we reject this alternative.

6. ~~**Dockerfile vendoring vs DCGM base image.**~~ **RESOLVED v1.3: vendor pydcgm.** §9 already describes the multi-stage build that copies the bindings from `nvcr.io/nvidia/cloud-native/dcgm`. ~5 MB image-size impact (the Python bindings + `libdcgm.so`), versus +400 MB for taking the full DCGM base image — and the existing PR #9682 image already has `pynvml` + `libnvidia-ml.so`, so the vendoring approach keeps one container image with two actuators rather than two base-image variants. Image-bake CI gains a layer; operators see no change.

7. ~~**Default selection mechanic was tried twice.**~~ Worth noting for review hygiene: this design ran through three default-selection proposals before landing on v1.2. v1's "default `nvml`, opt-in `dcgm`" was the conservative baseline. v1.1's "default `auto` with probe" was the convenience-optimal but ignored the §6.6 mutual-exclusion principle. v1.2 returns to v1's default with the §6.6 reasoning now explicit. The intermediate iteration is captured in the revision history; PR B reviewers don't need to re-litigate it unless they have a new argument that doesn't fit in §6.6.

---

## 12. Reference material

In-repo:
- `components/power_agent/power_agent.py:208-258` — the existing `_clamp_to_constraints` + `_apply_cap` that becomes `NvmlActuator.apply_cap`.
- `components/power_agent/power_agent.py:268-286` — `_handle_sigterm`'s restore block that becomes `NvmlActuator.restore_default`.
- `components/power_agent/power_agent.py:294-324` — `_restore_orphaned_gpus_on_startup`, migrated in v1.5 onto the actuator surface. Now takes an `Actuator` arg and dispatches all six operations (`device_count`, `get_uuid`, `list_running_pids`, `current_w`, `default_w`, `restore_default`) through it. The two guards from the inline NVML version — UUID gating against `_previously_managed` and the `current_w < default_w` skip — are preserved verbatim. The `current_w` / `default_w` Protocol additions exist expressly to keep the second guard intact across the migration (see §6.1 v1.5 additions and v1.5 changelog Fix #5). On the DCGM path the orphan-restore write now flows through `nvidia-dcgm` as a `dcgmConfigSet`, so the hostengine's "target configuration" record stays consistent with the driver-level cap — pre-v1.5 raw-NVML write would have desynced them.
- `components/power_agent/power_agent.py:399-431` — `PowerAgent.__init__`; gains an `Actuator` parameter.
- `deploy/helm/charts/power-agent/values.yaml:20-33` — `agent:` block that gains `actuator`, `dcgm.host`, `dcgm.port`, `dcgm.enforce`.
- `deploy/helm/charts/power-agent/templates/daemonset.yaml:66-71` — `command:` block that gains the new flags.
- `docs/design-docs/pr9369-split-plan.md` §2.1 — what shipped in PR9682 (the NVML-only Power Agent).

Upstream DCGM source tree (https://github.com/NVIDIA/DCGM), paths repo-relative:
- `dcgmlib/src/NvmlTaskRunnerGenerated.cpp:3513-3517` — `::nvmlDeviceSetPowerManagementLimit(device.nvmlDevice, limit)` is the bottom of `dcgmConfigSet` for power.
- `dcgmlib/entry_point.h:283-289` — `dcgmConfigSet` C entry point.
- `dcgmlib/entry_point.h:336-339` — `dcgmConfigEnforce` C entry point.
- `dcgmlib/dcgm_test_apis.h:236-239` — `dcgmConfigEnforce` semantics ("re-asserts the most recent dcgmConfigSet").
- `testing/python3/DcgmHandle.py:29-107` — `DcgmHandle.__init__` connection modes (embedded vs unixSocketPath vs ipAddress).
- `testing/python3/DcgmGroup.py:39-46` — `DcgmGroupConfig.Set(config)` wrapping `dcgm_agent.dcgmConfigSet`.
- `testing/python3/tests/test_configmanager.py:113-122` — canonical pattern for constructing `c_dcgmDeviceConfig_v2` with all-but-one field blanked.
- `testing/python3/tests/test_configmanager.py:265-268` — `mPowerLimit.type = DCGM_CONFIG_POWER_BUDGET_GROUP` / `mPowerLimit.val = watts` for group-budget; `DCGM_CONFIG_POWER_CAP_INDIVIDUAL` for per-GPU.
- `testing/python3/tests/test_bind_unbind_gpus.py:1216-1218` — DCGM test infra counting NVML calls behind each `set_power_limit(group, W)`, proving the 1:1 mapping at runtime.
- `testing/python3/dcgm_field_helpers.py:67-71` — **canonical decode** of `DCGM_FI_DEV_COMPUTE_PIDS` field-value: one `c_dcgmRunningProcess_t` per `c_dcgmFieldValue_v1`. Load-bearing evidence for §6.3 note 2 / Q3 resolution / v1.2 reversal of v1.1's full-DCGM PID plan.
- `testing/python3/tests/test_dcgm_reader.py:120-155` — `test_reading_pid_fields` showing the upstream pattern is to *loop* calling `GetLatestGpuValuesAsFieldIdDict` until a specific PID is seen, which confirms the field is time-series, not snapshot.
- `testing/python3/dcgm_structs.py:1844-1853` — `c_dcgmFieldValue_v1` has `version/fieldId/fieldType/status/ts/value` and the `value` union is `i64/dbl/str/blob`. **No `blob_size` field exists** — load-bearing for why v1.1's `raw.value.blob_size`-based decode was structurally wrong, not just semantically wrong.
- `dcgmlib/dcgmlib.linux_def` — exported symbols list. **Absence** of `dcgmGetDeviceProcesses` / `dcgmGetRunningProcesses` is the load-bearing evidence that the snapshot-PID API just doesn't exist in the public C API.
- `testing/python3/dcgm_fields.py:231-239` — the four-field power-limit surface: `DCGM_FI_DEV_POWER_MGMT_LIMIT = 160` (current), `_MIN = 161`, `_MAX = 162` (max settable), `_DEF = 163` (**factory default**, the field that matches `nvmlDeviceGetPowerManagementDefaultLimit`). Load-bearing for v1.4 D fix.
- `testing/python3/tests/test_connection.py:48-87` — kill nv-hostengine via `--term` or SIGKILL, next pydcgm API call raises `dcgmExceptionClass(DCGM_ST_CONNECTION_NOT_VALID)`. Load-bearing for v1.4 §6.3.1 stale-handle recovery pattern.
- `pydcgm/dcgmvalue.py:14-26` (installed at `/shared/pydcgm/dcgmvalue.py` under `datacenter-gpu-manager-4-core`) — the seven blank sentinels (`DCGM_INT32_BLANK = 0x7FFFFFF0`, `DCGM_INT64_BLANK = 0x7FFFFFFFFFFFFFF0`, `DCGM_FP64_BLANK = 140737488355328.0` = 2^47, `DCGM_STR_BLANK = "<<<NULL>>>"`, plus three NOT_SUPPORTED variants). **Load-bearing for v1.10 #1+#2+#3:** (a) the live constants are in `dcgmvalue`, NOT `dcgm_structs` (#3), (b) field-cache reads of unwatched fields return the type-appropriate blank rather than raising (`DCGM_STR_BLANK` for UUID → #1, `DCGM_FP64_BLANK` for power-limit fields → #2), so calling code can't tell "no consumer ever watched this" apart from "value really is blank" without a blank check or routing to a synchronous API.
- `dcgmlib/dcgm_agent.h` `dcgmGetDeviceAttributes` + `pydcgm/DcgmSystem.py` `DcgmSystemDiscovery.GetGpuAttributes(gpuId)` → `c_dcgmDeviceAttributes_v3{.identifiers.uuid, .powerLimits.{cur,default,enforced,min,max}PowerLimit, ...}`. **The synchronous device-info API that backs v1.10's UUID + power-limit reads.** Wraps a single hostengine RPC, returns populated from the discovery state without field-cache dependency.

Upstream NVIDIA (unchanged from v2.4):
- `nvidia/gpu-operator/values.yaml` — `dcgm.enabled: false` is still the default. Field-engineer evidence is about customer override, not upstream change.
- `nvidia/gpu-operator/assets/state-dcgm/0400_dcgm.yml` — `nvidia-dcgm` DaemonSet manifest, `privileged: true`, `containerPort: 5555`. Sourced by §8.2.

---

## 13. Summary

The v2.4 defer was correct on the deployment shape it analyzed. Field
evidence reveals a non-empty subset of customer clusters where the
defer's load-bearing premise (S2) doesn't hold. This doc adds the
DCGM actuator for that subset, behind:

- A clean `Actuator` Protocol that today's NVML code satisfies trivially.
- A `DcgmActuator` that uses DCGM for the write path and SKU constraints (UUID + power limits via `DcgmSystem.discovery.GetGpuAttributes(gpu_id)` — `.identifiers.uuid` and `.powerLimits.{cur,default,enforced,min,max}PowerLimit` — per the v1.10 fix that backed out the original field-cache path; write via `dcgmConfigSet` with `dcgmvalue.DCGM_INT32_BLANK` to leave non-power config fields untouched; optional `dcgmConfigEnforce` to register the cap as DCGM's target configuration for reset/reinit auto-reapply) and NVML for the snapshot-of-PIDs read (because DCGM has no public snapshot-PID API — see §6.3 note 2; the asymmetry is forced by upstream API shapes, not by design preference).
- A **strict binary actuator selection** (`agent.actuator: nvml | dcgm`, default `nvml`) declared at chart-install time, with no runtime probe and no auto-detection (v1.2 reversal of v1.1's `auto` mode, per §6.4 and §6.6).
- A **mutual-exclusion-by-construction guarantee** (§6.6): one chart, one DaemonSet, one pod per node, one actuator locked at startup. No two writer paths can co-exist by design; the chart's `validateActuator` guard catches typos at template time.
- An honest accounting (§7, §8) of what DCGM actually buys, **rewritten in v1.5** after a second source-grounded review:
  - At `enforce: false` (the default): writer-path routing through the customer's `nvidia-dcgm`, audit-log emission, API consistency with the customer's DCGM stack. Same observable cap-write behaviour as NVML otherwise.
  - At `enforce: true` (opt-in): all of the above, plus **automatic re-application of the cap after GPU reset/reinit** (the cap is registered as DCGM's "target configuration" per `DcgmConfigManager.h:113-117`).
  - What DCGM does **not** buy on either setting: Power Agent stays `privileged: true`; the cgroup parser still does PID→pod attribution because DCGM has no concept of K8s pods; NVML stays in the image for the snapshot-of-PIDs read regardless of actuator selection. The cap does **not** survive Power Agent restart (SIGTERM restores default; orphan recovery is a safety net for ungraceful death, not a survival mechanism). The cap does **not** get repaired faster than the 15-s reconcile against external `nvidia-smi -pl` clobbering (there is no continuous re-enforce loop in DCGM source — v1.x claimed otherwise; v1.5 corrects it).

The NVML path remains the default on clusters without `nvidia-dcgm`
and remains byte-identical to PR #9682. The DCGM path is
byte-equivalent at the silicon (§5.2, in-source proof) and adds
**one new resilience property** — automatic cap re-application after
GPU reset or reinit (only when `enforce: true`) — plus three
operational properties (single-writer routing, audit logging, `dcgmi`
visibility) for the customers who already pay the `nvidia-dcgm`
privileged-DS cost for their own reasons.

No production-default behaviour changes on default clusters. On
clusters where the operator has set `dcgm.enabled=true` in the GPU
Operator, switching to the DCGM actuator is an explicit one-Helm-
value action: `--set agent.actuator=dcgm` (and, if re-assertion is
also desired, `--set agent.dcgm.enforce=true`). Without that change,
a chart re-roll keeps the existing NVML actuator — the chart default
is `agent.actuator: nvml` (§6.5), by design (§6.4, §6.6: explicit
declaration over runtime probe).
