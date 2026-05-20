# SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

{{/*
Expand the name of the chart.
*/}}
{{- define "power-agent.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "power-agent.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "power-agent.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "power-agent.labels" -}}
helm.sh/chart: {{ include "power-agent.chart" . }}
{{ include "power-agent.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/component: power-agent
{{- end }}

{{/*
Selector labels
*/}}
{{- define "power-agent.selectorLabels" -}}
app.kubernetes.io/name: {{ include "power-agent.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Create the name of the service account to use
*/}}
{{- define "power-agent.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "power-agent.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Validate that image.tag is set. The :latest fallback was rejected on PR #9682
review (CodeRabbit comment on daemonset.yaml:58). Pin a release tag or
sha256:digest at install time:
    --set image.tag=v1.1.0
    --set image.tag=sha256:abc...
*/}}
{{- define "power-agent.validateImageTag" -}}
{{- if not .Values.image.tag -}}
{{- fail "image.tag is required (pin to a release tag or sha256:digest; :latest is not supported)" -}}
{{- end -}}
{{- end -}}

{{/*
Validate that production DaemonSet mode and in-cluster dev-pod mode are
not both enabled, and that dev mode has a pinned nodeName. Surfaces at
`helm install` / `helm template` time, not as two competing Pods at runtime.
*/}}
{{- define "power-agent.validateMutex" -}}
{{- if and .Values.daemonset.enabled .Values.dev.enabled -}}
{{- fail "daemonset.enabled and dev.enabled are mutually exclusive. Set exactly one." -}}
{{- end -}}
{{- if and .Values.dev.enabled (not .Values.dev.nodeName) -}}
{{- fail "dev.enabled requires dev.nodeName (the GPU node to pin the dev pod to)." -}}
{{- end -}}
{{- end -}}

{{/*
Validate the actuator selection. Catches typos at `helm install` /
`helm template` time so the operator doesn't discover their mistake
when the pod CrashLoopBackOffs with argparse's terse "invalid choice"
error.

The Power Agent's --actuator CLI flag has the same choices=["nvml",
"dcgm"] guard, but surfacing the error at template time gives a
clearer message and keeps a misconfigured install from ever creating
Pod objects (no leftover ImagePullBackOff, no leftover RBAC).
*/}}
{{- define "power-agent.validateActuator" -}}
{{- $a := .Values.agent.actuator | default "" -}}
{{- if not (or (eq $a "nvml") (eq $a "dcgm")) -}}
{{- fail (printf "agent.actuator must be 'nvml' or 'dcgm'; got %q" $a) -}}
{{- end -}}
{{- end -}}

{{/*
Validate agent.dcgm.enforce. Catches boolean typos (e.g.
--set agent.dcgm.enforce=treu) at `helm install` / `helm template`
time, so an operator doesn't lose an image-pull + container-start
round-trip discovering it via argparse's exit-with-error path.

The allowlist mirrors `power_agent._parse_bool_strict` so the chart
and the CLI agree on what's accepted. Comparison is case-insensitive
because Helm preserves user casing when stringifying YAML values
(`enforce: True` → "True", `--set …=TRUE` → "TRUE"); both are
expected to work.

Only validates when actuator=dcgm — the flag is only rendered onto
the pod command line in that case, so a stray value when
actuator=nvml is harmless (won't reach argparse).
*/}}
{{- define "power-agent.validateEnforce" -}}
{{- if eq (.Values.agent.actuator | default "") "dcgm" -}}
{{- $e := .Values.agent.dcgm.enforce | toString | lower | trim -}}
{{- $allowed := list "true" "1" "yes" "on" "false" "0" "no" "off" -}}
{{- if not (has $e $allowed) -}}
{{- fail (printf "agent.dcgm.enforce must be one of %v (case-insensitive); got %q. The Power Agent's --dcgm-enforce CLI flag uses the same allowlist (power_agent._parse_bool_strict)." $allowed .Values.agent.dcgm.enforce) -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
Effective RBAC scope.

Dev mode pins to one node and one namespace, so cluster-wide pod-listing
RBAC would be excessive. The agent's --namespace CLI flag
(power_agent.py:541-546) already constrains its pod queries to a single
namespace when set; the dev-pod template passes --namespace=$(POD_NAMESPACE)
via the downward API. This helper makes the RBAC default match the agent's
actual reach in dev mode.

An operator can still opt back into cluster-wide RBAC in dev mode by
setting --set dev.namespaceRestrictedOverride=true. Without that flag,
the dev-mode default wins regardless of rbac.namespaceRestricted.

Returns a Go-string "true" / "false" because Helm template conditionals
compare against the boolean values produced by YAML, but downstream
templates use it in `eq ... "true"` form for clarity.
*/}}
{{- define "power-agent.effectiveNamespaceRestricted" -}}
{{- if and .Values.dev.enabled (not .Values.dev.namespaceRestrictedOverride) -}}
true
{{- else -}}
{{- .Values.rbac.namespaceRestricted | toString -}}
{{- end -}}
{{- end -}}
