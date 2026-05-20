# SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Power Agent image — supports both NVML and DCGM actuator paths.
#
# Multi-stage build:
#   Stage 1 (dcgm-vendor) copies pydcgm Python bindings + libdcgm.so
#     from the upstream nvcr.io/nvidia/cloud-native/dcgm image. We
#     vendor (rather than pip-install) for two reasons: (1) pydcgm
#     isn't on PyPI, and (2) version pinning the Python bindings to
#     the libdcgm.so they ship with eliminates a class of dlopen
#     mismatch failures.
#   Stage 2 (runtime) layers the agent's Python deps + sources on top
#     of a slim Python base, then copies the vendored DCGM artifacts
#     into /opt/dcgm/. PYTHONPATH + LD_LIBRARY_PATH wire them up.
#
# The image still works when the agent runs in actuator: nvml mode
# (the chart default) — pydcgm is imported lazily by DcgmActuator
# methods, so an NVML deployment never opens libdcgm.so. The
# vendoring is dormant overhead (~12 MB image bloat) for NVML users
# and the only deployment path for DCGM users. See
# docs/design-docs/power-agent-dual-actuator.md §9 for the rationale.
#
# Build:
#   docker build -t nvcr.io/nvidia/dynamo/power-agent:vX.Y.Z \
#     -f components/power_agent/Dockerfile components/power_agent

# -----------------------------------------------------------------------------
# Stage 1: extract pydcgm + libdcgm.so from the upstream DCGM image
# -----------------------------------------------------------------------------
# Pin a specific DCGM version. Bump in lockstep with the GPU Operator's
# nvidia-dcgm tag — the hostengine and the bindings should share a
# major version (DCGM guarantees backward compat within a major, not
# across).
ARG DCGM_IMAGE=nvcr.io/nvidia/cloud-native/dcgm:4.2.3-2-ubuntu22.04
FROM ${DCGM_IMAGE} AS dcgm-vendor

# pydcgm bindings live under /usr/share/datacenter-gpu-manager/sdk_samples/scripts
# in the DCGM container image (these are the same bindings shipped under
# DCGM/testing/python3/ in the source tree). The library lives at
# /usr/lib/x86_64-linux-gnu/.
RUN set -eux; \
    mkdir -p /vendor/dcgm-python /vendor/dcgm-lib; \
    # Python bindings: flat layout — copy every .py from the SDK samples \
    # scripts dir (pydcgm.py imports from sibling modules like \
    # DcgmHandle.py, DcgmSystem.py, etc.). \
    cp -a /usr/local/dcgm/bindings/python3/*.py /vendor/dcgm-python/; \
    # libdcgm.so + module shared libs (DcgmActuator only needs the core, \
    # but the modules are dlopen()d by the hostengine on the *server* side \
    # which is in a separate Pod — we ship libdcgm.so + its versioned \
    # symlink so ctypes' default search resolves it on Linux). \
    cp -a /usr/lib/x86_64-linux-gnu/libdcgm.so* /vendor/dcgm-lib/

# -----------------------------------------------------------------------------
# Stage 2: runtime image
# -----------------------------------------------------------------------------
# Slim base — the NVIDIA Container Toolkit injects libnvidia-ml.so at
# runtime via runtimeClassName: nvidia (set by the Helm chart), so we
# don't bake pynvml's native dependency into the image itself.
FROM python:3.11-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # DCGM bindings live at /opt/dcgm/python; libdcgm.so lives at \
    # /opt/dcgm/lib. PYTHONPATH lets `import pydcgm` resolve when \
    # DcgmActuator runs; LD_LIBRARY_PATH lets the bindings' ctypes \
    # dlopen find libdcgm.so. Both are no-ops when --actuator=nvml. \
    PYTHONPATH=/opt/dcgm/python \
    LD_LIBRARY_PATH=/opt/dcgm/lib

# Python deps. pynvml + kubernetes + prometheus_client are PR #9682's
# existing requirements; nothing else is added on the DCGM path (the
# pydcgm bindings are pure-Python ctypes wrappers around libdcgm.so).
RUN pip install --no-cache-dir \
    pynvml==12.0.* \
    kubernetes==30.* \
    prometheus-client==0.20.*

# Vendored DCGM artifacts.
COPY --from=dcgm-vendor /vendor/dcgm-python /opt/dcgm/python
COPY --from=dcgm-vendor /vendor/dcgm-lib    /opt/dcgm/lib

# Agent sources.
WORKDIR /app
COPY power_agent.py actuator.py /app/

# Run as root — power-cap writes (NVML or DCGM) require it, and the
# agent persists managed-GPU UUID state to a hostPath. See chart
# securityContext block. The Helm chart sets runAsUser: 0 explicitly.
USER 0

# The Helm chart's command: line owns the actual argv (it injects the
# --actuator flag based on values.yaml). This ENTRYPOINT is for
# operators who run the image directly outside the chart (dev,
# debugging).
ENTRYPOINT ["python", "/app/power_agent.py"]
CMD ["--help"]
