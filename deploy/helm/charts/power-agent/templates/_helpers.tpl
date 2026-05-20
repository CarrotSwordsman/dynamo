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
Validate the production image reference. Exactly one of `image.tag` or
`image.digest` must be set. Rules per PR #9682 review (initial + two
follow-ups):

  * Both empty       → fail. Chart removed the `:latest` fallback
                       deliberately; the operator must pin.
  * Both set         → fail. Ambiguous which one wins; refuse
                       rather than silently preferring one.
  * Either value has leading/trailing whitespace → fail. OCI tags
                       and digests do not permit whitespace, and
                       silently trimming would mask an operator's
                       `--set-string 'image.tag= v1.1.0 '` typo
                       until the kubelet's pull fails. PR #9682
                       follow-up: `--set-string image.tag=" v1.1.0 "`
                       was previously rendering `repo: v1.1.0 ` (the
                       latest-comparison branch trimmed but the
                       render path used the raw value).
  * `tag == "latest"`→ fail. Case-insensitive (so
                       `--set image.tag=LATEST` fails too). The
                       comparison is against the literal canonical
                       form, NOT a substring — release tags that
                       happen to contain "latest" (e.g.
                       `v1.0.0-latest-rc`) are still accepted.
  * `tag` starts with `sha256:` → fail. Catches the original PR9682
                       bug of putting a digest on the tag field
                       (would render the invalid reference
                       `repo:sha256:...`).
  * `digest` not exactly `sha256:<64 hex>` → fail. SHA-256 is
                       always 64 hex chars (32 bytes × 2 nybbles).
                       Anything shorter or longer is a typo /
                       truncation and must be rejected — pre-fix
                       this allowed `{32,}` which accepted
                       half-digests like
                       `sha256:abc123def4567890abc123def4567890`
                       (32 chars). Per PR9682 follow-up.

A passing chart install is guaranteed to render either
`{repo}:{tag}` (no `latest`, no whitespace) or
`{repo}@sha256:<64 hex>` — both are valid OCI image references.
*/}}
{{- define "power-agent.validateImageTag" -}}
{{- $tag := .Values.image.tag | toString -}}
{{- $digest := .Values.image.digest | toString -}}
{{- if and (not $tag) (not $digest) -}}
{{- fail "image.tag or image.digest is required (pin to a release tag like v1.1.0 or a digest like sha256:abc...; :latest is not supported)" -}}
{{- end -}}
{{- if and $tag $digest -}}
{{- fail (printf "image.tag and image.digest are mutually exclusive (got tag=%q digest=%q). Pick one — digest gives strict content-addressed reproducibility, tag is human-readable." $tag $digest) -}}
{{- end -}}
{{- if $tag -}}
{{- if ne $tag (trim $tag) -}}
{{- fail (printf "image.tag=%q has leading/trailing whitespace; OCI tags do not permit whitespace and rendering it verbatim would produce an invalid image reference (e.g. `repo: v1.1.0 `). Fix the --set / values.yaml input. Hint: --set-string preserves whitespace exactly as quoted." $tag) -}}
{{- end -}}
{{- $tagLower := $tag | lower -}}
{{- if eq $tagLower "latest" -}}
{{- fail (printf "image.tag=%q is not supported. Pin a release tag (e.g. v1.1.0) or set image.digest=sha256:<hex>. The :latest tag was deliberately rejected on PR #9682 review to keep deployments reproducible." $tag) -}}
{{- end -}}
{{- if hasPrefix "sha256:" $tagLower -}}
{{- fail (printf "image.tag=%q looks like a digest (starts with sha256:) — digests must go on image.digest, NOT image.tag. Rendering %q:%q would produce an invalid OCI image reference (`repo:sha256:...` parses as repo + tag \"sha256\" with a stray suffix). Use `--set image.tag=\"\" --set image.digest=%s` instead. PR #9682 added image.digest as a separate field for exactly this reason." $tag .Values.image.repository $tag $tag) -}}
{{- end -}}
{{- end -}}
{{- if $digest -}}
{{- if ne $digest (trim $digest) -}}
{{- fail (printf "image.digest=%q has leading/trailing whitespace; OCI digests do not permit whitespace. Fix the --set / values.yaml input." $digest) -}}
{{- end -}}
{{- if not (regexMatch "^sha256:[0-9a-fA-F]{64}$" $digest) -}}
{{- fail (printf "image.digest=%q is not a valid SHA-256 digest. Must match sha256:<64 hex chars> exactly (SHA-256 is 32 bytes = 64 nybbles). Anything shorter is a truncated digest; anything longer is a typo. Example: image.digest=sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855. PR #9682 follow-up tightened this from {32,} to {64}." $digest) -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
Render the canonical image reference for a given repository / tag /
digest set. Used by daemonset.yaml + dev-pod.yaml so the two stay in
lockstep on digest handling.

  * `image.digest` set → `{repository}@{digest}` (OCI digest form)
  * else                → `{repository}:{tag}` (OCI tag form)

Assumes `validateImageTag` has already run on the same value tree
(both daemonset.yaml and dev-pod.yaml include it at the top of the
file), so we don't re-validate here.

Usage: `image: {{ include "power-agent.imageRef" (dict "repository"
.Values.image.repository "tag" .Values.image.tag "digest"
.Values.image.digest) | quote }}`

Validator guarantees both tag and digest are whitespace-free at
this point, so we render them verbatim — silent trimming here
would mask validator regressions.
*/}}
{{- define "power-agent.imageRef" -}}
{{- if .digest -}}
{{- printf "%s@%s" .repository .digest -}}
{{- else -}}
{{- printf "%s:%s" .repository .tag -}}
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
