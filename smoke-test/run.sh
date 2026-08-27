#!/usr/bin/env bash
#
# Local smoke test for the `send-test-activations` command.
#
# Runs the real CLI against a real (local) Kubernetes cluster to exercise the
# k8s API interaction the unit tests can only mock: reading the source
# Deployment, building the producer Job manifest (--dry-run), and actually
# creating + polling the Job.
#
# Usage:
#   colima start --kubernetes        # or: kind create cluster / minikube start
#   make smoke-test                  # or: ./smoke-test/run.sh
#
# Override the CLI invocation if the console script isn't on PATH:
#   CLI="uv run taskbroker-scripts" ./smoke-test/run.sh
set -euo pipefail

CLI="${CLI:-taskbroker-scripts}"
NS="${NS:-default}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MANIFESTS="$HERE/manifests"
SRC_DEPLOY="smoke-source-task-runner"
JOB_PREFIX="taskbroker-test-activations-"

# Safety: this creates and deletes Deployments and Jobs, so refuse to touch
# anything that doesn't look like a local dev cluster.
ctx="$(kubectl config current-context)"
case "$ctx" in
  kind-* | colima | minikube | docker-desktop | rancher-desktop | orbstack) ;;
  *)
    echo "Refusing to run against context '$ctx' — it doesn't look like a local cluster." >&2
    echo "Switch to kind/colima/minikube/docker-desktop first." >&2
    exit 1
    ;;
esac

echo "Using context '$ctx', namespace '$NS', CLI '$CLI'"

producer_jobs() {
  kubectl get jobs -n "$NS" -o name 2>/dev/null | grep "job.batch/${JOB_PREFIX}" || true
}

cleanup() {
  for j in $(producer_jobs); do
    kubectl delete "$j" -n "$NS" --ignore-not-found >/dev/null 2>&1 || true
  done
  kubectl delete -f "$MANIFESTS" -n "$NS" --ignore-not-found >/dev/null 2>&1 || true
}
trap cleanup EXIT
cleanup # start from a clean slate

# $CLI may be multi-word (e.g. "uv run taskbroker-scripts"), so leave it unquoted.
run() { $CLI send-test-activations --namespace "$NS" "$@"; }

echo
echo "== case 1: missing source Deployment should fail fast =="
if run --source-deployment doesnotexist --dry-run; then
  echo "FAIL: expected failure for a missing source Deployment" >&2
  exit 1
fi
echo "PASS: failed fast as expected"

kubectl apply -f "$MANIFESTS/source-deployment.yaml" -n "$NS"

echo
echo "== case 2: --dry-run builds a manifest and creates nothing =="
out="$(run --source-deployment "$SRC_DEPLOY" --dry-run --num-tasks 3)"
echo "$out" | grep -q '"kind": "Job"' \
  || { echo "FAIL: dry-run did not print a Job manifest" >&2; exit 1; }
echo "$out" | grep -q -- '--kafka-topic=taskworker-canary' \
  || { echo "FAIL: dry-run manifest missing the canary invocation" >&2; exit 1; }
if [ -n "$(producer_jobs)" ]; then
  echo "FAIL: --dry-run created a Job" >&2
  exit 1
fi
echo "PASS: manifest built, no Job created"

echo
echo "== case 3: real create -> Job is created and polled =="
# The stub's busybox image can't run taskbroker-send-tasks, so the Job fails and
# the CLI exits non-zero. What we're verifying is the real create + poll path:
# the CLI reaches the cluster, creates the Job, and reports its outcome.
if run --source-deployment "$SRC_DEPLOY" --num-tasks 1 --timeout 30 --check-interval 2; then
  echo "FAIL: expected non-zero (the stub image can't run taskbroker-send-tasks)" >&2
  exit 1
fi
created="$(producer_jobs)"
if [ -z "$created" ]; then
  echo "FAIL: no ${JOB_PREFIX}* Job was created" >&2
  exit 1
fi
echo "PASS: created and polled $created"

echo
echo "ALL SMOKE CHECKS PASSED"
