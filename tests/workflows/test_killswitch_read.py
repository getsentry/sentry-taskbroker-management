"""Tests for the read half of the taskbroker killswitch.

The write half is covered in `test_killswitch_change.py`.

The read step does two jobs: find the pools, and check that each one holds a
runtime-config this workflow can plan a change against.
"""

import json
from typing import Any, cast
from unittest import mock

import pytest
from kubernetes.client.rest import ApiException  # type: ignore[import-untyped]

from sentry_taskbroker_management.workflows import killswitch

NS = "default"
REGION = "us2"

# The text every rendered manifest holds today, because no region override
# defines `broker.runtime_config`. The bare key parses to an empty list.
BARE = "drop_task_killswitch:\ndemoted_namespaces:\n"


def _rendered(rules_json: str, extra: str = "") -> str:
    """
    The layout `_broker.runtime-configmap.yaml.j2` produces: the rule list as a
    one-line JSON array, then the other runtime-config keys.
    """
    return f"drop_task_killswitch: {rules_json}\ndemoted_namespaces: []\n{extra}"


def _cm(name: str, data: dict[str, str] | None) -> mock.MagicMock:
    m = mock.MagicMock()
    m.metadata.name = name
    m.data = data
    return m


def _pool_of(configmap_name: str) -> str | None:
    """The pool a broker runtime-config ConfigMap belongs to, or None."""
    prefix, suffix = "task-", "-broker-runtime-config"
    if configmap_name.startswith(prefix) and configmap_name.endswith(suffix):
        return configmap_name[len(prefix) : -len(suffix)] or None
    return None


def _run(
    configmaps: list[mock.MagicMock],
    pools: str = "all",
    namespace: str = NS,
    live_pools: list[str] | None = None,
    region: str = REGION,
) -> mock.MagicMock:
    """
    Run the read step against a fake cluster holding `configmaps`.

    `live_pools` is the ops repo's pool inventory for the region, which is what
    the step reads by name. It defaults to the pools `configmaps` looks like it
    holds, so a test that does not care about the difference between the repo and
    the cluster does not have to say it twice. Pass it to make them disagree.
    """
    by_name = {cm.metadata.name: cm for cm in configmaps}
    if live_pools is None:
        live_pools = sorted(
            pool for pool in (_pool_of(name) for name in by_name) if pool is not None
        )

    def _read(name: str, ns: str) -> mock.MagicMock:
        assert ns == namespace
        if name not in by_name:
            raise ApiException(status=404, reason="Not Found")
        return by_name[name]

    with (
        mock.patch("kubernetes.config.load_kube_config") as load_cfg,
        mock.patch("kubernetes.client.CoreV1Api") as Core,
    ):
        core = Core.return_value
        core.read_namespaced_config_map.side_effect = _read
        killswitch.list_killswitches(pools, region, json.dumps({region: live_pools}), namespace)
        load_cfg.assert_called_once()
        return cast(mock.MagicMock, core)


def _output() -> list[dict[str, Any]]:
    with open("/tmp/killswitches.json") as f:
        return cast(list[dict[str, Any]], json.load(f))


def _by_pool(report: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {entry["pool"]: entry for entry in report}


# --- finding the pools ------------------------------------------------------


def test_all_reads_every_pool_the_repo_declares_and_nothing_else() -> None:
    core = _run(
        [
            _cm("task-default-broker-runtime-config", {"runtime-config": BARE}),
            _cm("task-ingest-errors-broker-runtime-config", {"runtime-config": BARE}),
            _cm("task-rogue-broker-runtime-config", {"runtime-config": BARE}),
        ],
        live_pools=["default", "ingest-errors"],
    )
    report = _output()
    assert sorted(e["pool"] for e in report) == ["default", "ingest-errors"]
    assert all(e["rules"] == [] and e["problems"] == [] for e in report)
    assert sorted(c.args[0] for c in core.read_namespaced_config_map.call_args_list) == [
        "task-default-broker-runtime-config",
        "task-ingest-errors-broker-runtime-config",
    ]


def test_the_step_never_lists_the_namespace() -> None:
    core = _run([_cm("task-default-broker-runtime-config", {"runtime-config": BARE})])
    core.list_namespaced_config_map.assert_not_called()


def test_a_named_selection_reads_only_those_pools() -> None:
    core = _run(
        [
            _cm("task-default-broker-runtime-config", {"runtime-config": BARE}),
            _cm("task-ingest-errors-broker-runtime-config", {"runtime-config": BARE}),
            _cm("task-long-broker-runtime-config", {"runtime-config": BARE}),
        ],
        pools="long, default",
    )
    assert sorted(e["pool"] for e in _output()) == ["default", "long"]
    # Not read at all, rather than read and filtered out.
    assert sorted(c.args[0] for c in core.read_namespaced_config_map.call_args_list) == [
        "task-default-broker-runtime-config",
        "task-long-broker-runtime-config",
    ]


def test_a_pool_the_repo_does_not_declare_fails_and_names_the_ones_it_does() -> None:
    with pytest.raises(SystemExit) as e:
        _run(
            [_cm("task-default-broker-runtime-config", {"runtime-config": BARE})],
            pools="defualt",
        )
    assert "defualt" in str(e.value)
    assert "['default']" in str(e.value)


def test_a_named_pool_missing_from_the_cluster_fails() -> None:
    # Declared in the repo, absent from the cluster. The operator asked for this
    # pool by name, so it is an error rather than something to read past.
    with pytest.raises(SystemExit) as e:
        _run(
            [_cm("task-default-broker-runtime-config", {"runtime-config": BARE})],
            pools="long",
            live_pools=["default", "long"],
        )
    assert "long" in str(e.value)
    assert "wrong cluster" in str(e.value)


def test_all_skips_a_declared_pool_the_cluster_has_not_created(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _run(
        [_cm("task-default-broker-runtime-config", {"runtime-config": BARE})],
        live_pools=["default", "not-created-yet"],
    )
    assert [e["pool"] for e in _output()] == ["default"]
    assert "not-created-yet" in capsys.readouterr().out


def test_a_read_that_fails_for_any_other_reason_is_not_swallowed() -> None:
    # Only a 404 means "no such pool". A 403 is the Role being wrong, and it must
    # surface as itself rather than as an empty region.
    with (
        mock.patch("kubernetes.config.load_kube_config"),
        mock.patch("kubernetes.client.CoreV1Api") as Core,
    ):
        Core.return_value.read_namespaced_config_map.side_effect = ApiException(
            status=403, reason="Forbidden"
        )
        with pytest.raises(ApiException) as e:
            killswitch.list_killswitches("all", REGION, json.dumps({REGION: ["default"]}), NS)
    assert e.value.status == 403


def test_a_region_the_inventory_does_not_carry_fails() -> None:
    # `_run` always builds an inventory for the region it is given, so this one
    # calls the step directly to make the two disagree.
    with (
        mock.patch("kubernetes.config.load_kube_config"),
        mock.patch("kubernetes.client.CoreV1Api"),
    ):
        with pytest.raises(SystemExit) as e:
            killswitch.list_killswitches("all", "nowhere", json.dumps({REGION: ["default"]}), NS)
    assert "nowhere" in str(e.value)
    assert REGION in str(e.value)


def test_a_cluster_with_no_broker_configmaps_fails() -> None:
    with pytest.raises(SystemExit) as e:
        _run([_cm("some-other-configmap", {"runtime-config": BARE})], live_pools=["default"])
    assert "wrong cluster" in str(e.value)


def test_empty_pools_selection_fails() -> None:
    with pytest.raises(SystemExit) as e:
        _run([_cm("task-default-broker-runtime-config", {"runtime-config": BARE})], pools=" , ")
    assert "pools is required" in str(e.value)


def test_pools_that_parses_as_json_fails_with_a_readable_message() -> None:
    # Hera's preamble json-parses every parameter first, so `["default"]` typed
    # into the UI arrives as a list rather than as text.
    with pytest.raises(SystemExit) as e:
        _run(
            [_cm("task-default-broker-runtime-config", {"runtime-config": BARE})],
            pools=["default"],  # type: ignore[arg-type]
        )
    assert "arrived as a list" in str(e.value)


# --- reading the rules ------------------------------------------------------


def test_the_rules_are_reported_as_the_task_names_they_are() -> None:
    _run(
        [
            _cm(
                "task-default-broker-runtime-config",
                {
                    "runtime-config": _rendered(
                        '["sentry.replays.tasks.delete_recording_async", "sentry.tasks.unmerge"]',
                        extra="demoted_topic: taskworker-long\n",
                    )
                },
            )
        ]
    )
    entry = _by_pool(_output())["default"]
    assert entry["rules"] == [
        "sentry.replays.tasks.delete_recording_async",
        "sentry.tasks.unmerge",
    ]
    # The other runtime-config keys are read past, not reported as a problem.
    assert entry["problems"] == []


@pytest.mark.parametrize(
    "text",
    [
        # The bare key and an explicit empty list both mean "no killswitch".
        BARE,
        "drop_task_killswitch: []\ndemoted_namespaces:\n",
        _rendered('["a.b"]'),
        # Every other key is somebody else's, and the splice leaves them alone.
        _rendered('["a.b"]', extra="demoted_topic: t\ndemoted_topic_cluster: a,b\n"),
        "drop_task_killswitch: []\n",
    ],
)
def test_a_rendered_runtime_config_is_read_without_complaint(text: str) -> None:
    _run([_cm("task-default-broker-runtime-config", {"runtime-config": text})])
    assert _by_pool(_output())["default"]["problems"] == []


@pytest.mark.parametrize(
    "data,problem_fragment",
    [
        ({"runtime-config": "drop_task_killswitch: [\n"}, "not valid YAML"),
        ({"runtime-config": "just a string"}, "expected a mapping"),
        ({"runtime-config": "- a\n- b\n"}, "expected a mapping"),
        ({"runtime-config": ""}, "no `drop_task_killswitch` key"),
        ({"runtime-config": "demoted_namespaces:\n"}, "no `drop_task_killswitch` key"),
        ({"config": "x"}, "no `runtime-config` key"),
        (None, "no `runtime-config` key"),
        ({"runtime-config": _rendered("{a: 1}")}, "expected a list"),
        # The shapes taskbroker would read but this workflow does not write: a
        # selector rule, and anything that is not a name at all.
        (
            {"runtime-config": _rendered('[{"name": "a.b", "select_kwarg": {"project_id": 1}}]')},
            "are not task names",
        ),
        ({"runtime-config": _rendered("[123]")}, "are not task names"),
        ({"runtime-config": _rendered('[["a.b"]]')}, "are not task names"),
    ],
)
def test_a_runtime_config_this_workflow_cannot_read_fails_the_step(
    data: dict[str, str] | None, problem_fragment: str
) -> None:
    # Failing is the point. The write path plans against what this step read, so
    # a pool whose contents it cannot account for must not reach a patch.
    with pytest.raises(SystemExit) as e:
        _run([_cm("task-default-broker-runtime-config", {**data} if data else None)])
    assert "cannot read" in str(e.value)
    assert problem_fragment in _by_pool(_output())["default"]["problems"][0]


def test_the_failure_says_how_to_get_past_it() -> None:
    # `pools: all` is the default, so one pool in this state blocks the whole
    # region, and an operator mid-incident needs the way round it.
    with pytest.raises(SystemExit) as e:
        _run(
            [
                _cm("task-default-broker-runtime-config", {"runtime-config": _rendered('["a.b"]')}),
                _cm("task-long-broker-runtime-config", {"runtime-config": _rendered("[123]")}),
            ]
        )
    assert "['long']" in str(e.value)
    assert "instead of 'all'" in str(e.value)


def test_the_whole_report_is_written_before_the_step_fails() -> None:
    # One unreadable pool must not hide the others: an operator needs the whole
    # picture from one run, and the JSON output feeds the write path later.
    with pytest.raises(SystemExit):
        _run(
            [
                _cm("task-default-broker-runtime-config", {"runtime-config": _rendered('["a.b"]')}),
                _cm("task-long-broker-runtime-config", {"runtime-config": _rendered("[123]")}),
            ]
        )
    report = _by_pool(_output())
    assert report["default"]["rules"] == ["a.b"]
    assert report["default"]["problems"] == []
    assert report["long"]["problems"]
    # A pool the write path must not plan against hands over no parsed config.
    assert report["long"]["config"] is None


def test_the_step_writes_nothing_to_the_cluster() -> None:
    core = _run(
        [_cm("task-default-broker-runtime-config", {"runtime-config": _rendered('["a.b"]')})]
    )
    core.patch_namespaced_config_map.assert_not_called()
    core.replace_namespaced_config_map.assert_not_called()
    core.create_namespaced_config_map.assert_not_called()
    core.delete_namespaced_config_map.assert_not_called()
    # One `get` per declared pool, and no `list`. See
    # `test_the_step_never_lists_the_namespace`.
    core.list_namespaced_config_map.assert_not_called()
    core.read_namespaced_config_map.assert_called_once_with(
        "task-default-broker-runtime-config", NS
    )


def test_the_report_groups_rules_by_task_name(capsys: pytest.CaptureFixture[str]) -> None:
    _run(
        [
            _cm("task-default-broker-runtime-config", {"runtime-config": _rendered('["a.b"]')}),
            _cm("task-long-broker-runtime-config", {"runtime-config": _rendered('["a.b", "c.d"]')}),
            _cm("task-teapot-broker-runtime-config", {"runtime-config": BARE}),
        ]
    )
    out = capsys.readouterr().out
    assert "2 of 3 selected pool(s) hold killswitch rules." in out
    assert "a.b  (2 pool(s)): default, long" in out
    assert "c.d  (1 pool(s)): long" in out
