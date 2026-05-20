/*
 * SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
 * SPDX-License-Identifier: Apache-2.0
 */

package controller

import (
	"fmt"

	nvidiacomv1beta1 "github.com/ai-dynamo/dynamo/deploy/operator/api/v1beta1"
	"github.com/ai-dynamo/dynamo/deploy/operator/internal/checkpoint"
	"github.com/ai-dynamo/dynamo/deploy/operator/internal/dynamo"
)

// overlayServiceGMSRestoreClients applies the service's GMS runtime config to
// restore-time pods. The DynamoCheckpoint records save-time GMS wiring; restore
// client sidecars live on the service pod template.
func overlayServiceGMSRestoreClients(info *checkpoint.CheckpointInfo, serviceGMS *nvidiacomv1beta1.GPUMemoryServiceSpec) error {
	if info == nil || serviceGMS == nil {
		return nil
	}
	alphaServiceGMS := dynamo.ToAlphaGPUMemoryService(serviceGMS)
	if alphaServiceGMS == nil || !alphaServiceGMS.Enabled {
		return nil
	}
	if info.Exists && (info.GPUMemoryService == nil || !info.GPUMemoryService.Enabled) {
		return fmt.Errorf("gpuMemoryService restore requires resolved checkpoint %q to enable gpuMemoryService", info.CheckpointName)
	}
	if info.GPUMemoryService == nil || !info.GPUMemoryService.Enabled {
		return nil
	}
	info.GPUMemoryService = alphaServiceGMS.DeepCopy()
	return nil
}
