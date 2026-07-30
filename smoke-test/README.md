# pool-healthcheck smoke test

A local, cluster-backed smoke test for the `pool-healthcheck` command. The unit
tests in `tests/` mock the Kubernetes client; this runs the real CLI against a
real (local) cluster so you can verify the actual API interaction — Deployment
lookup by name, `spec.replicas` / `status.readyReplicas` handling, and gRPC
readiness-probe detection.

It is **not** run in CI (it needs a live cluster) — it's a manual dev aid.

## What it checks

`run.sh` walks the three behaviours that matter, asserting the CLI's exit code:

1. **Missing Deployment → times out.** No `task-doesnotexist-broker` exists, so
   the command polls and exits non-zero on timeout.
2. **No gRPC readiness probe → refused.** `manifests/pool-no-grpc.yaml` deploys a
   broker with no probe; the command fails fast ("Refusing to certify") rather
   than trusting `readyReplicas`.
3. **gRPC-ready pool → certified.** `manifests/pool-ready.yaml` uses the k8s
   `agnhost grpc-health-checking` server so pods pass a real gRPC readiness
   probe; `readyReplicas == replicas` and the command exits 0.

## Running

```bash
# 1. Start a local cluster (any of these):
colima start --kubernetes      # macOS
# kind create cluster
# minikube start

# 2. Install the CLI into the dev venv:
make install-ci                # or: uv sync --group dev

# 3. Run it:
make smoke-test
```

The script **refuses to run** unless your current kube-context looks local
(`kind-*`, `colima`, `minikube`, `docker-desktop`, `rancher-desktop`,
`orbstack`) — it creates and deletes Deployments, so it must never point at a
shared cluster. It cleans up the Deployments it creates on exit.

Overrides:

- `CLI="uv run taskbroker-scripts" ./smoke-test/run.sh` if the console script
  isn't on your `PATH`.
- `NS=<namespace> make smoke-test` to use a namespace other than `default`.

## Relationship to the real health check

In production this command runs as a Job inside the target region's cluster (via
the ops workflow-engine `RunRemoteContainer` procedure) and authenticates with
the pod's ServiceAccount (`load_incluster_config`). Locally it falls back to your
kubeconfig. The end-to-end path across both clusters + GCP credentials can only
be exercised as a dry-run in a real region (e.g. s4s2) — see STREAM-1158.
