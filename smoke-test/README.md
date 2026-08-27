# send-test-activations smoke test

A local, cluster-backed smoke test for the `send-test-activations` command. The
unit tests in `tests/` mock the Kubernetes client; this runs the real CLI
against a real (local) cluster so you can verify the actual API interaction —
reading the source Deployment, building the producer Job from its pod template,
and creating + polling the Job.

It is **not** run in CI (it needs a live cluster) — it's a manual dev aid.

## What it checks

`run.sh` walks three behaviours, asserting the CLI's exit code:

1. **Missing source Deployment → fails fast.** No `doesnotexist` Deployment, so
   the command exits non-zero instead of building a Job from nothing.
2. **`--dry-run` builds a manifest and creates nothing.**
   `manifests/source-deployment.yaml` provides a readable (0-replica) source
   Deployment; the command prints the producer Job manifest (checked for the
   `taskworker-canary` invocation) and creates no Job.
3. **Real create → Job is created and polled.** The command creates the producer
   Job in the cluster and polls it. The stub's busybox image can't run
   `taskbroker-send-tasks`, so the Job fails and the CLI exits non-zero — the
   point is that the real create + poll path is exercised and a
   `taskbroker-test-activations-*` Job actually shows up.

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
`orbstack`) — it creates and deletes Deployments and Jobs, so it must never
point at a shared cluster. It cleans up what it creates on exit.

Overrides:

- `CLI="uv run taskbroker-scripts" ./smoke-test/run.sh` if the console script
  isn't on your `PATH`.
- `NS=<namespace> make smoke-test` to use a namespace other than `default`.

## Relationship to the real run

In production this command runs as a Job inside the target region's cluster (via
the ops workflow-engine `RunRemoteContainer` procedure) and authenticates with
the pod's ServiceAccount (`load_incluster_config`). Locally it falls back to your
kubeconfig. The full path — real getsentry image producing to the canary topic,
a validating pool consuming, worker → AlloyDB — can only be exercised in a real
region (e.g. s4s2); see STREAM-1781.
