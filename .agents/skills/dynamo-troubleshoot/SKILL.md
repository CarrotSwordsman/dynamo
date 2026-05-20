---
name: dynamo-troubleshoot
description: Diagnose and recover Dynamo recipe, router, Kubernetes deployment, model-cache, endpoint, and benchmark failures. Use when pods are not ready, a recipe fails to deploy, the frontend returns errors, workers are missing, routing is unhealthy, or a benchmark job fails.
---

# Dynamo Troubleshoot

## Goal

Turn a Dynamo failure into a clear problem class, strongest signal, and next
action. Start with read-only evidence, avoid secrets, and fix one layer at a
time.

## Workflow

### 1. Collect A Read-Only Bundle

Run:

```bash
python3 .agents/skills/dynamo-troubleshoot/scripts/collect_dynamo_debug_bundle.py \
  --namespace "${NAMESPACE}" \
  --outdir /tmp/dynamo-debug
```

If the user names a deployment, include it:

```bash
python3 .agents/skills/dynamo-troubleshoot/scripts/collect_dynamo_debug_bundle.py \
  --namespace "${NAMESPACE}" \
  --deployment-name <deployment-name>
```

Do not collect Kubernetes secrets. Do not print Hugging Face tokens.

### 2. Classify The Failure

Use `references/failure-decision-tree.md` and classify into one primary bucket:

- cluster/platform
- namespace/secret
- model cache/PVC/download
- image pull/runtime image
- GPU scheduling/resources
- operator/DynamoGraphDeployment reconciliation
- frontend/router
- worker/backend
- endpoint/API
- benchmark/perf job

### 3. Debug Top Down

Check in this order:

1. namespace, storage class, GPU nodes, and HF secret existence
2. PVC and model-download job
3. `DynamoGraphDeployment` status and events
4. pod status, `describe pod`, and container logs
5. frontend service and port-forward
6. `/v1/models`
7. `/v1/chat/completions`
8. benchmark job only after endpoint smoke test passes

### 4. Fix One Layer At A Time

Prefer the smallest reversible change:

- create missing namespace or HF secret
- patch `storageClassName`
- patch image tag or image pull secret
- reduce GPU request only if the recipe can still be valid
- switch KV router to approximate mode only if workers do not publish events
- restart failed jobs after fixing the underlying config

After each fix, rerun the relevant readiness check before moving deeper.

## Output Contract

Return:

- problem class
- evidence checked
- strongest signal
- likely cause
- exact next command or patch
- what was ruled out
- whether it is safe to continue deployment or benchmarking

## References

- Read `references/failure-decision-tree.md` for bucket-specific checks.
- Use `scripts/collect_dynamo_debug_bundle.py` for read-only bundle collection.
