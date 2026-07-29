import time

import click
from kubernetes import client, config  # type: ignore[import-untyped]


class HealthcheckTimeoutError(Exception):
    """Raised when a pool's Deployment does not become ready within the timeout."""


def _load_kube_config() -> None:
    """Load in-cluster config when running as a Job, falling back to a local kubeconfig.

    The health check normally runs as a Job inside the target region's cluster, so it
    authenticates with the pod's ServiceAccount. The kubeconfig fallback is for running
    the command locally against a cluster you already have credentials for.
    """
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()


def _pool_ready(api: client.AppsV1Api, deployment_name: str, namespace: str) -> bool:
    """Return True once every replica of the pool's broker Deployment is Ready.

    Readiness is gated on the taskbroker gRPC ConsumerService probe, so a ready pod is
    also ``ConsumerService=Serving`` and ``readyReplicas == replicas`` doubles as a gRPC
    health check. Refuses to certify a Deployment whose readiness probe is not gRPC.

    A 404 (Deployment not created / still rolling out) is transient and returns False;
    a 401/403 (missing or unapplied RBAC) will not resolve, so it fails fast.
    """
    try:
        dep = api.read_namespaced_deployment(deployment_name, namespace)
    except client.exceptions.ApiException as exc:
        if exc.status == 404:
            click.echo(
                f"Deployment '{deployment_name}' not found in ns '{namespace}' yet",
                err=True,
            )
            return False
        if exc.status in (401, 403):
            raise SystemExit(
                f"k8s API returned {exc.status} reading '{deployment_name}' in ns "
                f"'{namespace}'. The pod's RBAC is likely missing or not yet applied."
            )
        raise
    desired = dep.spec.replicas
    ready_replicas = dep.status.ready_replicas or 0
    probe = dep.spec.template.spec.containers[0].readiness_probe
    if probe is None or probe.grpc is None:
        raise SystemExit(
            f"Deployment '{deployment_name}' has no gRPC readinessProbe; readyReplicas "
            "would not reflect taskbroker gRPC health. Refusing to certify."
        )
    click.echo(f"  {deployment_name}: {ready_replicas}/{desired} ready", err=True)
    return bool(desired) and ready_replicas == desired


@click.command()
@click.option(
    "-p",
    "--pool-name",
    required=True,
    help="Name of the taskbroker pool to health-check.",
)
@click.option(
    "-n",
    "--namespace",
    default="default",
    show_default=True,
    help="Namespace the pool's broker Deployment runs in.",
)
@click.option(
    "-t",
    "--timeout",
    default=600,
    show_default=True,
    type=int,
    help="How long in seconds to wait for the pool to become ready before failing.",
)
@click.option(
    "-i",
    "--check-interval",
    default=10,
    show_default=True,
    type=int,
    help="How often in seconds to poll the Deployment's readiness.",
)
def pool_healthcheck(
    pool_name: str,
    namespace: str,
    timeout: int,
    check_interval: int,
) -> None:
    """Health-check a taskbroker pool.

    Blocks in a loop until every replica of the pool's broker Deployment
    (``task-<pool_name>-broker``, selected by name) is Ready, or the timeout is reached.
    """
    deployment_name = f"task-{pool_name}-broker"
    _load_kube_config()
    api = client.AppsV1Api()
    deadline = time.monotonic() + timeout
    while True:
        try:
            if _pool_ready(api, deployment_name, namespace):
                click.echo(
                    f"pool '{pool_name}' healthy: all replicas ready and gRPC-serving",
                    err=True,
                )
                break
        except client.exceptions.ApiException as exc:
            # transient (5xx / network); 401/403 already raised inside _pool_ready
            click.echo(f"transient k8s API error, retrying: {exc}", err=True)
        if time.monotonic() >= deadline:
            raise HealthcheckTimeoutError(
                f"timed out after {timeout}s waiting for pool '{pool_name}' "
                f"(deployment '{deployment_name}') in ns '{namespace}' to become ready"
            )
        time.sleep(check_interval)
