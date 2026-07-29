#!/usr/bin/env bash
#
# Local smoke test for the `pool-healthcheck` command.
#
# Runs the real CLI against a real (local) Kubernetes cluster to exercise the
# k8s API interaction that the unit tests can only mock: the missing-Deployment
# timeout, the no-gRPC-probe refusal, and the happy path where a gRPC-ready
# pool is certified healthy.
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

# Safety: this creates and deletes Deployments, so refuse to touch anything that
# doesn't look like a local dev cluster.
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

cleanup() {
  kubectl delete -f "$MANIFESTS" -n "$NS" --ignore-not-found >/dev/null 2>&1 || true
}
trap cleanup EXIT
cleanup # start from a clean slate

# $CLI may be multi-word (e.g. "uv run taskbroker-scripts"), so leave it unquoted.
run() { $CLI pool-healthcheck "$@"; }

echo
echo "== case 1: missing Deployment should time out =="
if run --pool-name doesnotexist --namespace "$NS" --timeout 3 --check-interval 1; then
  echo "FAIL: expected a timeout for a missing Deployment" >&2
  exit 1
fi
echo "PASS: timed out as expected"

echo
echo "== case 2: Deployment without a gRPC readiness probe should be refused =="
kubectl apply -f "$MANIFESTS/pool-no-grpc.yaml" -n "$NS"
kubectl rollout status deploy/task-nogrpc-broker -n "$NS" --timeout=60s
if run --pool-name nogrpc --namespace "$NS" --timeout 5 --check-interval 1; then
  echo "FAIL: expected refusal for a non-gRPC readiness probe" >&2
  exit 1
fi
echo "PASS: refused to certify as expected"

echo
echo "== case 3: gRPC-ready pool should be certified healthy =="
kubectl apply -f "$MANIFESTS/pool-ready.yaml" -n "$NS"
kubectl rollout status deploy/task-smoke-broker -n "$NS" --timeout=120s
run --pool-name smoke --namespace "$NS" --timeout 60 --check-interval 2
echo "PASS: certified healthy"

echo
echo "ALL SMOKE CHECKS PASSED"
