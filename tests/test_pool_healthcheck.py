from types import SimpleNamespace
from unittest import mock

from click.testing import CliRunner, Result
from kubernetes import client  # type: ignore[import-untyped]

from sentry_taskbroker_management.scripts.pools.healthcheck import (
    HealthcheckTimeoutError,
    pool_healthcheck,
)

HEALTHCHECK = "sentry_taskbroker_management.scripts.pools.healthcheck"


def _deployment(
    replicas: int | None, ready_replicas: int, has_grpc_probe: bool = True
) -> SimpleNamespace:
    """Build a minimal stand-in for a V1Deployment with the attributes we read."""
    probe = SimpleNamespace(grpc=object()) if has_grpc_probe else None
    container = SimpleNamespace(readiness_probe=probe)
    return SimpleNamespace(
        spec=SimpleNamespace(
            replicas=replicas,
            template=SimpleNamespace(spec=SimpleNamespace(containers=[container])),
        ),
        status=SimpleNamespace(ready_replicas=ready_replicas),
    )


def _run(read_side_effect: object) -> Result:
    """Invoke the command with a mocked AppsV1Api and no real cluster auth."""
    api = mock.Mock()
    api.read_namespaced_deployment.side_effect = read_side_effect
    with (
        mock.patch(f"{HEALTHCHECK}._load_kube_config"),
        mock.patch(f"{HEALTHCHECK}.client.AppsV1Api", return_value=api),
    ):
        return CliRunner().invoke(
            pool_healthcheck,
            ["--pool-name", "default", "--timeout", "0", "--check-interval", "0"],
        )


def test_pool_healthy_immediately() -> None:
    result = _run(read_side_effect=[_deployment(replicas=2, ready_replicas=2)])
    assert result.exit_code == 0, result.output
    assert "healthy" in result.output


def test_times_out_when_deployment_missing() -> None:
    not_found = client.exceptions.ApiException(status=404)
    result = _run(read_side_effect=not_found)
    assert result.exit_code != 0
    assert isinstance(result.exception, HealthcheckTimeoutError)


def test_times_out_when_not_enough_replicas_ready() -> None:
    result = _run(read_side_effect=[_deployment(replicas=3, ready_replicas=1)])
    assert result.exit_code != 0
    assert isinstance(result.exception, HealthcheckTimeoutError)


def test_fails_fast_on_forbidden() -> None:
    forbidden = client.exceptions.ApiException(status=403)
    result = _run(read_side_effect=forbidden)
    # 403 raises SystemExit rather than polling until timeout.
    assert result.exit_code != 0
    assert isinstance(result.exception, SystemExit)


def test_fails_fast_on_none_replicas() -> None:
    result = _run(read_side_effect=[_deployment(replicas=None, ready_replicas=0)])
    # spec.replicas=None is unexpected for a live Deployment; fail fast, don't time out.
    assert result.exit_code != 0
    assert isinstance(result.exception, SystemExit)


def test_refuses_deployment_without_grpc_probe() -> None:
    deployment = _deployment(replicas=1, ready_replicas=1, has_grpc_probe=False)
    result = _run(read_side_effect=[deployment])
    assert result.exit_code != 0
    assert isinstance(result.exception, SystemExit)
