from __future__ import annotations

import argparse
import sys
import time

from kubernetes import client  # type: ignore[import-untyped]

from sentry_taskbroker_management.scripts.pools._kube import load_kube_config

CANARY_TOPIC = "taskworker-canary"
CANARY_TASK_FUNCTION_PATH = "canary_task"
CANARY_NAMESPACE = "internal"
JOB_NAME_PREFIX = "taskbroker-test-activations-"


class ProducerJobTimeoutError(Exception):
    """Raised when the producer Job does not complete within the timeout."""


class ProducerJobFailedError(Exception):
    """Raised when the producer Job runs but fails."""


def _canary_args(num_tasks: int) -> list[str]:
    """`--repeat` replaces the source Deployment's `--infinite` so it sends `num_tasks`
    and exits."""
    return [
        "run",
        "taskbroker-send-tasks",
        f"--task-function-path={CANARY_TASK_FUNCTION_PATH}",
        f"--namespace={CANARY_NAMESPACE}",
        f"--kafka-topic={CANARY_TOPIC}",
        f"--repeat={num_tasks}",
    ]


def _build_producer_job(deployment: object, container_name: str, num_tasks: int) -> client.V1Job:
    """Turn a getsentry Deployment's pod template into a one-time producer Job, reusing its
    image/env/secrets/mounts/labels and overriding only what a one-shot producer needs.

    Init containers are dropped because the source's init-geoip busybox blocks forever
    waiting for a GeoIP file this Job never gets; the pod labels are kept because
    NetworkPolicy keys Kafka access off them.
    """
    template = deployment.spec.template  # type: ignore[attr-defined]
    pod_spec = template.spec

    matching = [c for c in pod_spec.containers if c.name == container_name]
    if not matching:
        names = [c.name for c in pod_spec.containers]
        raise SystemExit(
            f"container '{container_name}' not found in source deployment pod "
            f"(containers: {names})."
        )
    container = matching[0]
    container.args = _canary_args(num_tasks)
    container.readiness_probe = None
    container.liveness_probe = None
    container.startup_probe = None
    container.ports = None

    pod_spec.containers = [container]
    pod_spec.init_containers = None
    pod_spec.restart_policy = "Never"

    return client.V1Job(
        api_version="batch/v1",
        kind="Job",
        metadata=client.V1ObjectMeta(generate_name=JOB_NAME_PREFIX),
        spec=client.V1JobSpec(
            backoff_limit=0,
            ttl_seconds_after_finished=600,
            template=template,
        ),
    )


def _read_source_deployment(
    api: client.AppsV1Api, source_deployment: str, namespace: str
) -> object:
    try:
        return api.read_namespaced_deployment(source_deployment, namespace)
    except client.exceptions.ApiException as exc:
        if exc.status == 404:
            raise SystemExit(
                f"source deployment '{source_deployment}' not found in ns '{namespace}'. "
                "It provides the pod spec (getsentry image, Kafka producer creds, config "
                "mounts) the producer Job is built from; pass --source-deployment to point "
                "at a different one."
            )
        if exc.status in (401, 403):
            raise SystemExit(
                f"k8s API returned {exc.status} reading deployment '{source_deployment}' "
                f"in ns '{namespace}'. The pod's RBAC is likely missing or not yet applied."
            )
        raise


def _job_finished(api: client.BatchV1Api, job_name: str, namespace: str) -> bool:
    """True once the Job has succeeded; raises if it failed. A post-create 404 is transient
    (returns False); 401/403 fail fast."""
    try:
        status = api.read_namespaced_job_status(job_name, namespace).status
    except client.exceptions.ApiException as exc:
        if exc.status == 404:
            return False
        if exc.status in (401, 403):
            raise SystemExit(
                f"k8s API returned {exc.status} reading job '{job_name}' in ns "
                f"'{namespace}'. The pod's RBAC is likely missing or not yet applied."
            )
        raise
    if (status.failed or 0) > 0:
        raise ProducerJobFailedError(
            f"producer job '{job_name}' failed (status.failed={status.failed})"
        )
    return (status.succeeded or 0) > 0


def add_subparser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "send-test-activations",
        help="Seed the canary topic with test activations for pool-migration validation.",
        description=(
            "Build a one-time k8s Job from an existing getsentry Deployment's pod spec, "
            "running `sentry run taskbroker-send-tasks` to produce --num-tasks canary tasks "
            "to the shared taskworker-canary topic, then block until the Job completes."
        ),
    )
    parser.add_argument(
        "-n",
        "--num-tasks",
        type=int,
        default=100,
        help="Number of canary test activations to produce (default: 100).",
    )
    parser.add_argument(
        "--namespace",
        default="default",
        help="Namespace of the source deployment and the created producer Job (default: default).",
    )
    parser.add_argument(
        "--source-deployment",
        default="getsentry-consumer-example-task-runner-production",
        help="getsentry Deployment whose pod spec the producer Job is built from.",
    )
    parser.add_argument(
        "--container",
        dest="container_name",
        default="sentry",
        help="Container in the source pod to base the producer Job on (default: sentry).",
    )
    parser.add_argument(
        "-t",
        "--timeout",
        type=int,
        default=600,
        help="How long in seconds to wait for the producer Job to complete (default: 600).",
    )
    parser.add_argument(
        "-i",
        "--check-interval",
        type=int,
        default=10,
        help="How often in seconds to poll the Job's status (default: 10).",
    )
    parser.set_defaults(func=run)


def run(args: argparse.Namespace) -> None:
    """Seed the canary topic with test activations for pool-migration validation.

    A pool being validated (one that declares a `validation` block) consumes those tasks from
    the canary topic, exercising the full push path before it takes real traffic.
    """
    num_tasks: int = args.num_tasks
    if num_tasks <= 0:
        raise SystemExit(f"--num-tasks must be a positive integer, got {num_tasks}")

    load_kube_config()
    apps = client.AppsV1Api()
    batch = client.BatchV1Api()

    deployment = _read_source_deployment(apps, args.source_deployment, args.namespace)
    job = _build_producer_job(deployment, args.container_name, num_tasks)

    try:
        created = batch.create_namespaced_job(args.namespace, job)
    except client.exceptions.ApiException as exc:
        if exc.status in (401, 403):
            raise SystemExit(
                f"k8s API returned {exc.status} creating a job in ns '{args.namespace}'. "
                "The pod's RBAC is likely missing or not yet applied."
            )
        raise
    job_name = created.metadata.name
    print(
        f"created producer job '{job_name}' in ns '{args.namespace}': "
        f"{num_tasks} tasks -> '{CANARY_TOPIC}'",
        file=sys.stderr,
    )

    deadline = time.monotonic() + args.timeout
    while True:
        try:
            if _job_finished(batch, job_name, args.namespace):
                print(
                    f"producer job '{job_name}' completed: {num_tasks} test activations sent",
                    file=sys.stderr,
                )
                break
        except client.exceptions.ApiException as exc:
            # transient; 401/403 already fail fast inside _job_finished
            print(f"transient k8s API error, retrying: {exc}", file=sys.stderr)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProducerJobTimeoutError(
                f"timed out after {args.timeout}s waiting for producer job '{job_name}' "
                f"in ns '{args.namespace}' to complete"
            )
        time.sleep(min(args.check_interval, remaining))
