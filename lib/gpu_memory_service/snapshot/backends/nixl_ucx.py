# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""NIXL UCX restore backend for peer VRAM -> local GMS VRAM transfers."""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from typing import Any, Mapping, Sequence

from gpu_memory_service.snapshot.backends.nixl_common import (
    NIXL_UCX_BACKEND,
    VRAM_MEM_TYPE,
    NixlTransferResources,
    create_nixl_agent,
    load_nixl_api,
    release_nixl_transfer_resources,
    run_bounded_nixl_transfers,
)
from gpu_memory_service.snapshot.transfer import (
    NIXL_UCX_TRANSFER_BACKEND,
    GMSSnapshotConfig,
    GMSTransferTarget,
    RemoteTransferSource,
    TransferSession,
    validate_transfer_targets,
)

logger = logging.getLogger(__name__)

GMS_NIXL_UCX_CONFIG_PATH_ENV = "GMS_NIXL_UCX_CONFIG_PATH"
NIXL_UCX_CONFIG_PATH_CONFIG_KEY = "nixl_ucx_config_path"
NIXL_UCX_REMOTE_AGENT_CONFIG_KEY = "nixl_ucx_remote_agent"
NIXL_UCX_REMOTE_METADATA_CONFIG_KEY = "nixl_ucx_remote_metadata"
NIXL_UCX_SOURCES_CONFIG_KEY = "nixl_ucx_sources"


class NixlUCXTransferBackend:
    """NIXL UCX backend for restoring GMS allocations from peer GPU memory."""

    name = NIXL_UCX_TRANSFER_BACKEND

    def __init__(self, *, config: GMSSnapshotConfig) -> None:
        api = load_nixl_api()
        self._device = config.device
        self._max_workers = config.max_workers
        self._agent_name = f"gms_ucx_loader_{self._device}_{os.getpid()}"
        self._agent = create_nixl_agent(
            api,
            agent_name=self._agent_name,
            backend_name=NIXL_UCX_BACKEND,
        )

        remote_metadata = load_remote_peer_metadata(config.backend_config)["metadata"]
        if not remote_metadata:
            raise RuntimeError(
                f"{NIXL_UCX_TRANSFER_BACKEND} requires peer NIXL metadata in "
                f"{NIXL_UCX_REMOTE_METADATA_CONFIG_KEY}"
            )
        self._remote_agent = _load_remote_metadata(self._agent, remote_metadata)
        logger.info(
            "NIXL UCX backend initialized for device %d with peer %s and %d max "
            "in-flight transfers",
            self._device,
            self._remote_agent,
            self._max_workers,
        )

    def start_restore(self, sources: Sequence[RemoteTransferSource]) -> TransferSession:
        materialized_sources = [
            (
                RemoteTransferSource(
                    allocation_id=source.allocation_id,
                    remote_agent=self._remote_agent,
                    va=source.va,
                    device=source.device,
                    byte_count=source.byte_count,
                )
                if not source.remote_agent
                else source
            )
            for source in sources
        ]
        return _NixlUCXTransferSession(
            agent=self._agent,
            remote_agent=self._remote_agent,
            device=self._device,
            max_workers=self._max_workers,
            sources=materialized_sources,
        )

    def close(self) -> None:
        if self._agent is not None:
            try:
                self._agent.remove_remote_agent(self._remote_agent)
            except Exception:
                logger.debug(
                    "failed to remove UCX remote agent %s",
                    self._remote_agent,
                    exc_info=True,
                )
        self._agent = None


class _NixlUCXTransferSession:
    def __init__(
        self,
        *,
        agent: Any,
        remote_agent: str,
        device: int,
        max_workers: int,
        sources: Sequence[RemoteTransferSource],
    ) -> None:
        self._agent = agent
        self._remote_agent = remote_agent
        self._device = device
        self._max_workers = max(1, int(max_workers))
        self._sources = list(sources)
        self._active = True

    def restore(self, targets: Mapping[str, GMSTransferTarget]) -> None:
        validate_transfer_targets(self._sources, targets, device=self._device)
        self._validate_remote_agents()
        total_bytes = sum(source.byte_count for source in self._sources)
        t0 = time.monotonic()
        try:
            run_bounded_nixl_transfers(
                agent=self._agent,
                backend_name=NIXL_UCX_TRANSFER_BACKEND,
                items=self._sources,
                max_inflight=self._max_workers,
                prepare_transfer=lambda source: self._prepare_source_transfer(
                    source, targets[source.allocation_id]
                ),
                logger=logger,
            )
        finally:
            self._active = False

        elapsed = time.monotonic() - t0
        throughput = total_bytes / elapsed / (1024**3) if elapsed > 0 else 0
        logger.info(
            "NIXL UCX transfers complete: %.2f GiB in %.3fs "
            "(%.2f GiB/s, allocations=%d, max_inflight=%d)",
            total_bytes / (1024**3),
            elapsed,
            throughput,
            len(self._sources),
            self._max_workers,
        )

    def close(self) -> None:
        self._active = False

    def _validate_remote_agents(self) -> None:
        for source in self._sources:
            if source.remote_agent != self._remote_agent:
                raise RuntimeError(
                    f"{NIXL_UCX_TRANSFER_BACKEND} loaded peer {self._remote_agent!r} "
                    f"but source {source.allocation_id} references "
                    f"{source.remote_agent!r}"
                )

    def _prepare_source_transfer(
        self,
        source: RemoteTransferSource,
        target: GMSTransferTarget,
    ) -> NixlTransferResources:
        local_reg = None
        local_descs = self._agent.get_xfer_descs(
            [(target.va, target.byte_count, target.device)],
            mem_type=VRAM_MEM_TYPE,
        )
        remote_descs = self._agent.get_xfer_descs(
            [(source.va, source.byte_count, source.device)],
            mem_type=VRAM_MEM_TYPE,
        )
        handle = None
        transfer = None
        try:
            local_reg = self._agent.register_memory(
                [(target.va, target.byte_count, target.device, "")],
                VRAM_MEM_TYPE,
                backends=[NIXL_UCX_BACKEND],
            )
            handle = self._agent.initialize_xfer(
                "READ",
                local_descs,
                remote_descs,
                source.remote_agent,
                backends=[NIXL_UCX_BACKEND],
            )
            transfer = NixlTransferResources(
                handle=handle,
                label=source.allocation_id,
                registrations=(local_reg,),
            )
            return transfer
        except Exception:
            if transfer is not None:
                release_nixl_transfer_resources(self._agent, transfer)
            else:
                release_nixl_transfer_resources(
                    self._agent,
                    NixlTransferResources(
                        handle=handle,
                        label=source.allocation_id,
                        registrations=(() if local_reg is None else (local_reg,)),
                    ),
                )
            raise


def _load_remote_metadata(agent: Any, metadata: bytes | str) -> str:
    if isinstance(metadata, str):
        metadata = _decode_metadata_bytes(metadata)
    remote_name = agent.add_remote_agent(metadata)
    if isinstance(remote_name, bytes):
        return remote_name.decode("utf-8")
    return str(remote_name)


def load_remote_peer_metadata(config: Mapping[str, Any]) -> dict[str, Any]:
    """Load and normalize UCX peer metadata from config or JSON file."""
    payload: dict[str, Any] = {}
    path = config.get(NIXL_UCX_CONFIG_PATH_CONFIG_KEY) or os.environ.get(
        GMS_NIXL_UCX_CONFIG_PATH_ENV
    )
    if path:
        with open(path, encoding="utf-8") as handle:
            payload.update(json.load(handle))
    for key in (
        NIXL_UCX_REMOTE_AGENT_CONFIG_KEY,
        NIXL_UCX_REMOTE_METADATA_CONFIG_KEY,
        NIXL_UCX_SOURCES_CONFIG_KEY,
        "agent_name",
        "metadata",
        "sources",
    ):
        if key in config and config[key] is not None:
            payload[key] = config[key]

    agent_name = (
        payload["agent_name"]
        if "agent_name" in payload
        else payload.get(NIXL_UCX_REMOTE_AGENT_CONFIG_KEY)
    )
    metadata = (
        payload["metadata"]
        if "metadata" in payload
        else payload.get(NIXL_UCX_REMOTE_METADATA_CONFIG_KEY)
    )
    sources = (
        payload["sources"]
        if "sources" in payload
        else payload.get(NIXL_UCX_SOURCES_CONFIG_KEY)
    )

    if metadata is None:
        raise RuntimeError(
            f"{NIXL_UCX_TRANSFER_BACKEND} requires peer metadata "
            f"({NIXL_UCX_REMOTE_METADATA_CONFIG_KEY} or metadata)"
        )
    if isinstance(metadata, str):
        metadata = _decode_metadata_bytes(metadata)

    return {
        "agent_name": None if agent_name is None else str(agent_name),
        "metadata": metadata,
        "sources": sources,
    }


def _decode_metadata_bytes(value: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except Exception:
        return value.encode("latin1")
