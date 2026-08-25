import contextlib
import io
from types import SimpleNamespace
from typing import Any
from unittest import mock

from kubernetes import client  # type: ignore[import-untyped]

from sentry_taskbroker_management.cli import build_parser
from sentry_taskbroker_management.scripts.pools.test_activations import (
    ProducerJobFailedError,
    ProducerJobTimeoutError,
)

MOD = "sentry_taskbroker_management.scripts.pools.test_activations"


def _deployment(container_name: str = "sentry") -> SimpleNamespace:
    """A minimal stand-in for the getsentry source Deployment we copy the pod spec from."""
    container = SimpleNamespace(
        name=container_name,
        command=None,
        args=["run", "taskbroker-send-tasks", "--task-function-path=x", "--infinite"],
        readiness_probe=object(),
        liveness_probe=object(),
        startup_probe=object(),
        ports=[object()],
        env=[],
        volume_mounts=[],
    )
    pod_spec = SimpleNamespace(
        containers=[container],
        init_containers=[SimpleNamespace(name="init-geoip")],
        restart_policy=None,
    )
    template = SimpleNamespace(
        metadata=SimpleNamespace(labels={"system": "kafka_consumer"}),
        spec=pod_spec,
    )
    return SimpleNamespace(spec=SimpleNamespace(template=template))


def _status(succeeded: int = 0, failed: int = 0) -> SimpleNamespace:
    return SimpleNamespace(status=SimpleNamespace(succeeded=succeeded, failed=failed))


def _created(name: str = "taskbroker-test-activations-abc12") -> SimpleNamespace:
    return SimpleNamespace(metadata=SimpleNamespace(name=name))


class _Result:
    def __init__(self, exit_code: int, exception: BaseException | None, output: str) -> None:
        self.exit_code = exit_code
        self.exception = exception
        self.output = output


def _run(
    *,
    read_side_effect: object = None,
    create_return: object = None,
    status_return: object = None,
    create_side_effect: object = None,
    extra_args: list[str] | None = None,
) -> tuple[_Result, mock.Mock]:
    """Parse + dispatch the send-test-activations subcommand with mocked Apps/Batch APIs."""
    apps = mock.Mock()
    apps.read_namespaced_deployment.side_effect = (
        read_side_effect if read_side_effect is not None else [_deployment()]
    )
    batch = mock.Mock()
    if create_side_effect is not None:
        batch.create_namespaced_job.side_effect = create_side_effect
    else:
        batch.create_namespaced_job.return_value = create_return or _created()
    batch.read_namespaced_job_status.return_value = status_return or _status(succeeded=1)

    args = build_parser().parse_args(
        ["send-test-activations", "--timeout", "0", "--check-interval", "0", *(extra_args or [])]
    )
    buf = io.StringIO()
    with (
        mock.patch(f"{MOD}.load_kube_config"),
        mock.patch(f"{MOD}.client.AppsV1Api", return_value=apps),
        mock.patch(f"{MOD}.client.BatchV1Api", return_value=batch),
        contextlib.redirect_stderr(buf),
    ):
        try:
            args.func(args)
            result = _Result(0, None, buf.getvalue())
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
            result = _Result(code, exc, buf.getvalue())
        except BaseException as exc:
            result = _Result(1, exc, buf.getvalue())
    return result, batch


def _submitted_job(batch: mock.Mock) -> Any:
    """The V1Job passed to create_namespaced_job(namespace, job)."""
    return batch.create_namespaced_job.call_args.args[1]


def test_completes_when_job_succeeds() -> None:
    result, _ = _run(status_return=_status(succeeded=1))
    assert result.exit_code == 0
    assert "completed" in result.output


def test_builds_bounded_canary_invocation() -> None:
    result, batch = _run(extra_args=["--num-tasks", "5"])
    assert result.exit_code == 0
    args = _submitted_job(batch).spec.template.spec.containers[0].args
    assert args == [
        "run",
        "taskbroker-send-tasks",
        "--task-function-path=canary_task",
        "--namespace=internal",
        "--kafka-topic=taskworker-canary",
        "--repeat=5",
    ]


def test_strips_pod_spec_for_one_shot_job() -> None:
    _, batch = _run()
    pod_spec = _submitted_job(batch).spec.template.spec
    # sidecars/init containers dropped so the pod can terminate; single target container left.
    assert len(pod_spec.containers) == 1
    assert pod_spec.init_containers is None
    assert pod_spec.restart_policy == "Never"
    container = pod_spec.containers[0]
    assert container.readiness_probe is None
    assert container.liveness_probe is None
    assert container.startup_probe is None
    assert container.ports is None


def test_job_spec_runs_once_and_self_cleans() -> None:
    _, batch = _run()
    job = _submitted_job(batch)
    assert job.spec.backoff_limit == 0
    assert job.spec.ttl_seconds_after_finished == 600
    assert job.metadata.generate_name == "taskbroker-test-activations-"


def test_rejects_nonpositive_num_tasks() -> None:
    result, batch = _run(extra_args=["--num-tasks", "0"])
    assert result.exit_code != 0
    assert isinstance(result.exception, SystemExit)
    batch.create_namespaced_job.assert_not_called()


def test_fails_when_source_deployment_missing() -> None:
    not_found = client.exceptions.ApiException(status=404)
    result, batch = _run(read_side_effect=not_found)
    assert result.exit_code != 0
    assert isinstance(result.exception, SystemExit)
    batch.create_namespaced_job.assert_not_called()


def test_fails_fast_on_forbidden_read() -> None:
    forbidden = client.exceptions.ApiException(status=403)
    result, _ = _run(read_side_effect=forbidden)
    assert result.exit_code != 0
    assert isinstance(result.exception, SystemExit)


def test_fails_when_container_not_found() -> None:
    result, batch = _run(extra_args=["--container", "nope"])
    assert result.exit_code != 0
    assert isinstance(result.exception, SystemExit)
    batch.create_namespaced_job.assert_not_called()


def test_fails_fast_on_forbidden_create() -> None:
    forbidden = client.exceptions.ApiException(status=403)
    result, _ = _run(create_side_effect=forbidden)
    assert result.exit_code != 0
    assert isinstance(result.exception, SystemExit)


def test_raises_when_job_fails() -> None:
    result, _ = _run(status_return=_status(succeeded=0, failed=1))
    assert result.exit_code != 0
    assert isinstance(result.exception, ProducerJobFailedError)


def test_times_out_when_job_never_completes() -> None:
    result, _ = _run(status_return=_status(succeeded=0, failed=0))
    assert result.exit_code != 0
    assert isinstance(result.exception, ProducerJobTimeoutError)
