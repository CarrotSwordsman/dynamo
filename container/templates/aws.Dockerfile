{#
# SPDX-FileCopyrightText: Copyright (c) 2024-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#}
# === BEGIN templates/aws.Dockerfile ===
#############################
########## AWS EFA ##########
#############################
#
# This stage extends the runtime/dev stage with the AWS EFA userspace stack:
#   1. AWS EFA installer (libfabric + aws-ofi-nccl plugin) — stock binaries
#   2. Patched aws/libfabric installed OVER the stock binary — adds the CUDA
#      dmabuf MR fix that makes fi_mr_reg succeed when registering VRAM on
#      GB200 EFA hardware. Without this, the stock EFA libfabric falls through
#      to ibv_reg_mr() with a GPU VA and returns EFAULT.
#
# Use this stage when deploying on AWS infrastructure with EFA support
# (p6e-gb200, p5e, p5, p4d).

FROM ${EFA_BASE_IMAGE} AS aws

ARG EFA_VERSION
ARG PATCHED_LIBFABRIC_REPO
ARG PATCHED_LIBFABRIC_REF

{% if target == "runtime" %}
USER root
{% endif %}

# Install AWS EFA installer with bundled libfabric and aws-ofi-nccl
# Flags explanation:
#   --skip-kmod: Skip kernel module installation (handled by host)
#   --skip-limit-conf: Skip ulimit configuration (handled by container runtime)
#   --no-verify: Skip GPG verification (optional, can be removed if verification is needed)
# Cache apt downloads; sharing=locked avoids apt/dpkg races with concurrent builds.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    mkdir -p /tmp/efa && \
    cd /tmp/efa && \
    curl --retry 3 --retry-delay 2 -fsSL -o aws-efa-installer-${EFA_VERSION}.tar.gz \
        https://efa-installer.amazonaws.com/aws-efa-installer-${EFA_VERSION}.tar.gz && \
    tar -xf aws-efa-installer-${EFA_VERSION}.tar.gz && \
    cd aws-efa-installer && \
    apt-get update && \
    ./efa_installer.sh -y --skip-kmod --skip-limit-conf --no-verify && \
    rm -rf /tmp/efa && \
    rm -rf /opt/amazon/aws-ofi-nccl && \
    ldconfig

# Build and install patched aws/libfabric over the stock EFA installer binary.
#
# The EFA installer ships stock libfabric (e.g., 2.4.0amzn1.0 in 1.46.0). Its
# CUDA HMEM path falls through to ibv_reg_mr() with a GPU virtual address and
# fails with EFAULT on GB200 VRAM registration. The aws/libfabric v2.3.1amzn4
# fork carries the CUDA dmabuf MR fix (corresponds to upstream PR
# https://github.com/ofiwg/libfabric/pull/12216).
#
# `make install` does NOT update the libfabric.so.1 SONAME symlink; the
# pre-existing symlink from EFA installer 1.46.0 still points at the stock
# 1.30.x binary. We force the symlink in BOTH /opt/amazon/efa/lib and
# /opt/amazon/efa/lib64 (EFA installer can populate either depending on
# distro) AND delete the stock 1.30.x binaries to defend against apps with
# hardcoded RPATHs. Validation: fi_info --version must report the patched
# libfabric version at runtime — fail the build if not.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        autoconf automake libtool make build-essential pkg-config \
        libnl-3-dev libnl-route-3-dev libnuma-dev libibverbs-dev rdma-core \
        ca-certificates git && \
    mkdir -p /usr/local/src && cd /usr/local/src && \
    git clone --depth 1 --branch ${PATCHED_LIBFABRIC_REF} \
        ${PATCHED_LIBFABRIC_REPO} libfabric-patched && \
    cd libfabric-patched && \
    ./autogen.sh && \
    ./configure --prefix=/opt/amazon/efa \
                --enable-efa \
                --with-cuda=/usr/local/cuda \
                --enable-cuda-dlopen \
                --disable-verbs \
                --disable-psm3 \
                --disable-opx \
                --disable-usnic && \
    make -j"$(nproc)" && \
    make install && \
    cd /usr/local/src && rm -rf libfabric-patched && \
    # Force libfabric.so.1 SONAME to point at the patched binary in BOTH lib dirs.
    PATCHED_SONAME=$(basename "$(readlink /opt/amazon/efa/lib/libfabric.so)") && \
    [ -n "${PATCHED_SONAME}" ] || { echo "ERROR: could not resolve patched libfabric SONAME via /opt/amazon/efa/lib/libfabric.so" >&2; exit 1; } && \
    for libdir in /opt/amazon/efa/lib /opt/amazon/efa/lib64; do \
        [ -d "$libdir" ] || continue; \
        if [ -f "$libdir/$PATCHED_SONAME" ]; then \
            ln -sfT "$PATCHED_SONAME" "$libdir/libfabric.so.1"; \
        else \
            ln -sfT "/opt/amazon/efa/lib/$PATCHED_SONAME" "$libdir/libfabric.so.1"; \
        fi; \
        # Delete stock 1.30.x binaries to defend against hardcoded RPATHs.
        for stock in "$libdir"/libfabric.so.1.30.*; do \
            [ -e "$stock" ] && rm -f "$stock"; \
        done; \
    done && \
    ldconfig && \
    # Validate runtime libfabric is the patched build. Fail the build if not.
    if ! /opt/amazon/efa/bin/fi_info --version 2>&1 | grep -q "^libfabric: ${PATCHED_LIBFABRIC_REF#v}"; then \
        echo "ERROR: runtime libfabric did not resolve to patched ${PATCHED_LIBFABRIC_REF}" >&2; \
        /opt/amazon/efa/bin/fi_info --version >&2 || true; \
        exit 1; \
    fi && \
    echo "[aws] runtime libfabric verified as patched ${PATCHED_LIBFABRIC_REF}" && \
    rm -rf /var/lib/apt/lists/*

ENV EFA_VERSION="${EFA_VERSION}"
ENV PATCHED_LIBFABRIC_VERSION="${PATCHED_LIBFABRIC_REF}"

{% if target == "runtime" %}
USER dynamo
{% endif %}

ENTRYPOINT ["/opt/nvidia/nvidia_entrypoint.sh"]
CMD []
