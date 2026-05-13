/*
 * SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
 * SPDX-License-Identifier: Apache-2.0
 */

package controller

import (
	"context"
	"fmt"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/event"
	"sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/predicate"

	"github.com/ai-dynamo/dynamo/deploy/operator/internal/consts"
)

// TopologyLabelReconciler watches worker pods that have the
// nvidia.com/topology-label-key annotation. When a pod is scheduled
// (spec.nodeName is set) but the target label is missing, the controller
// reads the label from the node and patches it onto the pod. The Downward
// API volume then picks up the label value.
type TopologyLabelReconciler struct {
	client.Client
}

// +kubebuilder:rbac:groups="",resources=pods,verbs=get;list;watch;patch
// +kubebuilder:rbac:groups="",resources=nodes,verbs=get

func (r *TopologyLabelReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	logger := log.FromContext(ctx)

	var pod corev1.Pod
	if err := r.Get(ctx, req.NamespacedName, &pod); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}

	labelKey, ok := pod.Annotations[consts.KubeAnnotationTopologyLabelKey]
	if !ok || labelKey == "" {
		return ctrl.Result{}, nil
	}

	// Already has the label.
	if _, exists := pod.Labels[labelKey]; exists {
		return ctrl.Result{}, nil
	}

	// Not yet scheduled; reconcile again when spec.nodeName is set.
	if pod.Spec.NodeName == "" {
		return ctrl.Result{}, nil
	}

	var node corev1.Node
	if err := r.Get(ctx, types.NamespacedName{Name: pod.Spec.NodeName}, &node); err != nil {
		return ctrl.Result{}, fmt.Errorf("get node %s: %w", pod.Spec.NodeName, err)
	}

	labelValue, exists := node.Labels[labelKey]
	if !exists {
		logger.Info("Node missing topology label, skipping",
			"node", pod.Spec.NodeName, "labelKey", labelKey, "pod", req.NamespacedName)
		return ctrl.Result{}, nil
	}

	patch := client.MergeFrom(pod.DeepCopy())
	if pod.Labels == nil {
		pod.Labels = make(map[string]string)
	}
	pod.Labels[labelKey] = labelValue
	if err := r.Patch(ctx, &pod, patch); err != nil {
		return ctrl.Result{}, fmt.Errorf("patch pod label: %w", err)
	}

	logger.Info("Copied node topology label to pod",
		"pod", req.NamespacedName, "node", pod.Spec.NodeName,
		"label", labelKey, "value", labelValue)
	return ctrl.Result{}, nil
}

func (r *TopologyLabelReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&corev1.Pod{}).
		WithEventFilter(topologyLabelPredicate()).
		Complete(r)
}

// topologyLabelPredicate filters to annotated, scheduled pods that still need
// the target topology label copied from the node.
func topologyLabelPredicate() predicate.Predicate {
	return predicate.Funcs{
		CreateFunc: func(e event.CreateEvent) bool {
			return needsTopologyLabelCopy(e.Object)
		},
		UpdateFunc: func(e event.UpdateEvent) bool {
			return needsTopologyLabelCopy(e.ObjectNew)
		},
		DeleteFunc: func(_ event.DeleteEvent) bool {
			return false
		},
		GenericFunc: func(_ event.GenericEvent) bool {
			return false
		},
	}
}

func needsTopologyLabelCopy(obj client.Object) bool {
	pod, ok := obj.(*corev1.Pod)
	if !ok || pod == nil {
		return false
	}

	labelKey := pod.GetAnnotations()[consts.KubeAnnotationTopologyLabelKey]
	if labelKey == "" || pod.Spec.NodeName == "" {
		return false
	}

	_, exists := pod.GetLabels()[labelKey]
	return !exists
}
