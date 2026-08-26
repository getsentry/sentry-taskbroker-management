"""Tests for the write half of the taskbroker killswitch.

The read half is covered in `test_killswitch_read.py`. These cover the steps
that turn a rule into a patched ConfigMap: planning the change, writing it,
reading it back, and saying what the ops repo has to change.
"""

import json
from typing import Any, cast
from unittest import mock

import pytest
import yaml

from sentry_taskbroker_management.workflows import killswitch

NS = "default"

# The text every rendered manifest holds today, because no region override
# defines `broker.runtime_config`. Both bare keys parse to an empty list.
BARE = "drop_task_killswitch:\ndemoted_namespaces:\n"


def _entry(pool: str, config: dict[str, Any], text: str) -> dict[str, Any]:
    """One element of the report `list_killswitches` hands the write path."""
    return {
        "pool": pool,
        "configmap": f"task-{pool}-broker-runtime-config",
        "status": "ok",
        "rules": [],
        "problems": [],
        "text": text,
        "config": config,
    }


def _bare_entry(pool: str, rules: list[str] | None = None) -> dict[str, Any]:
    if rules is None:
        return _entry(pool, {"drop_task_killswitch": None, "demoted_namespaces": None}, BARE)
    text = f"drop_task_killswitch: {json.dumps(rules, sort_keys=True)}\ndemoted_namespaces:\n"
    return _entry(pool, {"drop_task_killswitch": rules, "demoted_namespaces": None}, text)


def _plan(report: list[dict[str, Any]], action: str, rule: object) -> list[dict[str, Any]]:
    killswitch.plan_change(json.dumps(report), action, json.dumps(rule, sort_keys=True))
    with open("/tmp/plan.json") as f:
        return cast(list[dict[str, Any]], json.load(f))


def _changed_flag() -> str:
    with open("/tmp/changed.txt") as f:
        return f.read()


def _by_pool(plan: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {item["pool"]: item for item in plan}


# --- plan_change: the add and remove arithmetic ----------------------------


def test_add_to_an_empty_list_renders_the_template_layout() -> None:
    plan = _plan([_bare_entry("default")], "add", "sentry.tasks.unmerge")
    assert plan[0]["changed"] is True
    assert plan[0]["new_text"] == (
        'drop_task_killswitch: ["sentry.tasks.unmerge"]\ndemoted_namespaces:\n'
    )
    assert _changed_flag() == "true"


def test_add_a_duplicate_warns_and_changes_nothing() -> None:
    # Not an error. Say so, so nobody reads an unchanged diff as a failed apply.
    plan = _plan([_bare_entry("default", ["sentry.tasks.unmerge"])], "add", "sentry.tasks.unmerge")
    assert plan[0]["changed"] is False
    assert "already present" in plan[0]["note"]
    # Nothing to approve and nothing to apply, so the workflow skips the pause.
    assert _changed_flag() == "false"


def test_remove_the_last_rule_writes_the_bare_key_not_an_empty_list() -> None:
    # `[]` would leave a one-line difference against every committed manifest,
    # and the drift job would report it three times a day for ever. Both forms
    # parse to an empty list, so this is about drift, not about behavior.
    plan = _plan(
        [_bare_entry("default", ["sentry.tasks.unmerge"])], "remove", "sentry.tasks.unmerge"
    )
    assert plan[0]["changed"] is True
    assert plan[0]["new_text"] == BARE


def test_remove_leaves_the_other_rules_alone() -> None:
    entry = _bare_entry("default", ["a.b", "sentry.tasks.unmerge", "c.d"])
    plan = _plan([entry], "remove", "sentry.tasks.unmerge")
    assert plan[0]["new_text"] == 'drop_task_killswitch: ["a.b", "c.d"]\ndemoted_namespaces:\n'


def test_remove_that_matches_no_pool_at_all_fails() -> None:
    # The failure worth being loud about: it looks exactly like a successful
    # removal, and the operator walks away believing the task is running again.
    with pytest.raises(SystemExit) as e:
        _plan([_bare_entry("default"), _bare_entry("long")], "remove", "sentry.tasks.unmerge")
    assert "nothing to remove" in str(e.value)


def test_remove_across_pools_only_needs_one_pool_to_match() -> None:
    # The default blast radius is every pool in the region and a task usually
    # runs in a few of them, so most pools legitimately have nothing to remove.
    plan = _by_pool(
        _plan(
            [_bare_entry("default", ["sentry.tasks.unmerge"]), _bare_entry("long")],
            "remove",
            "sentry.tasks.unmerge",
        )
    )
    assert plan["default"]["changed"] is True
    assert plan["long"]["changed"] is False
    assert "no rule equal to this one" in plan["long"]["note"]


def test_a_pool_with_nothing_to_remove_is_left_alone_byte_for_byte() -> None:
    # A pool this run does not change is not written at all, so what it holds
    # comes back out of the plan unchanged, down to the byte.
    untouched = 'drop_task_killswitch: ["a.b"]\ndemoted_namespaces:\n- sentry\n'
    plan = _by_pool(
        _plan(
            [
                _bare_entry("default", ["sentry.tasks.unmerge"]),
                _entry(
                    "long",
                    {"drop_task_killswitch": ["a.b"], "demoted_namespaces": ["sentry"]},
                    untouched,
                ),
            ],
            "remove",
            "sentry.tasks.unmerge",
        )
    )
    assert plan["default"]["changed"] is True

    assert plan["long"]["changed"] is False
    assert "no rule equal to this one" in plan["long"]["note"]
    assert plan["long"]["new_text"] == untouched


def test_a_block_list_layout_is_refused_rather_than_mangled() -> None:
    # The splice replaces one line, because the rendered value is one line. A
    # block list under the key is a layout nothing in this repository writes,
    # and splicing it would leave its items orphaned below the new line. The
    # round-trip check is what stops that reaching a ConfigMap.
    block_layout = "drop_task_killswitch:\n  - a.b\ndemoted_namespaces:\n"
    with pytest.raises(SystemExit) as e:
        _plan(
            [
                _entry(
                    "long",
                    {"drop_task_killswitch": ["a.b"], "demoted_namespaces": None},
                    block_layout,
                )
            ],
            "add",
            "sentry.tasks.unmerge",
        )
    assert "not valid YAML" in str(e.value)


def test_the_other_runtime_config_keys_survive_a_change() -> None:
    # The splice replaces the rule line and leaves every other byte alone, which
    # is what keeps `demoted_topic_cluster` intact. This step cannot recompute
    # it: the ops template derives it from topicctl at render time.
    rendered = (
        'drop_task_killswitch: ["a.b"]\ndemoted_namespaces:\n- sentry\ndemoted_topic: some-topic\n'
    )
    plan = _plan(
        [
            _entry(
                "long",
                {
                    "drop_task_killswitch": ["a.b"],
                    "demoted_namespaces": ["sentry"],
                    "demoted_topic": "some-topic",
                },
                rendered,
            )
        ],
        "add",
        "sentry.tasks.unmerge",
    )
    assert plan[0]["changed"] is True
    assert plan[0]["new_text"] == (
        'drop_task_killswitch: ["a.b", "sentry.tasks.unmerge"]\n'
        "demoted_namespaces:\n"
        "- sentry\n"
        "demoted_topic: some-topic\n"
    )


def test_add_touches_every_selected_pool() -> None:
    plan = _by_pool(
        _plan([_bare_entry("default"), _bare_entry("long")], "add", "sentry.tasks.unmerge")
    )
    assert all(item["changed"] for item in plan.values())


# --- plan_change: what the splice leaves alone -----------------------------


def test_demoted_namespaces_are_kept_as_a_block_list() -> None:
    config = {"drop_task_killswitch": None, "demoted_namespaces": ["ns1", "ns2"]}
    text = "drop_task_killswitch:\ndemoted_namespaces:\n- ns1\n- ns2\n"
    plan = _plan([_entry("default", config, text)], "add", "sentry.tasks.unmerge")
    assert plan[0]["new_text"] == (
        'drop_task_killswitch: ["sentry.tasks.unmerge"]\ndemoted_namespaces:\n- ns1\n- ns2\n'
    )


def test_every_other_line_is_left_exactly_as_it_was() -> None:
    config = {
        "drop_task_killswitch": None,
        "demoted_namespaces": ["ns1"],
        "demoted_topic_cluster": "b1:9092,b2:9092",
        "demoted_topic": "taskworker-demoted",
    }
    text = (
        "drop_task_killswitch:\ndemoted_namespaces:\n- ns1\n"
        "demoted_topic_cluster: b1:9092,b2:9092\ndemoted_topic: taskworker-demoted\n"
    )
    plan = _plan([_entry("default", config, text)], "add", "sentry.tasks.unmerge")
    assert plan[0]["new_text"] == (
        'drop_task_killswitch: ["sentry.tasks.unmerge"]\ndemoted_namespaces:\n- ns1\n'
        "demoted_topic_cluster: b1:9092,b2:9092\ndemoted_topic: taskworker-demoted\n"
    )


def test_a_key_this_workflow_cannot_recompute_is_kept() -> None:
    # `demoted_topic_cluster` is derived from topicctl at render time, so no
    # step here could put it back if the splice dropped it. Replacing one line
    # and leaving every other byte alone is what keeps it.
    config = {
        "drop_task_killswitch": None,
        "demoted_namespaces": None,
        "demoted_topic_cluster": "b1:9092,b2:9092",
        "demoted_topic": "taskworker-demoted",
    }
    text = (
        "drop_task_killswitch:\ndemoted_namespaces:\n"
        "demoted_topic_cluster: b1:9092,b2:9092\ndemoted_topic: taskworker-demoted\n"
    )
    plan = _plan([_entry("default", config, text)], "add", "sentry.tasks.unmerge")
    assert "demoted_topic_cluster: b1:9092,b2:9092" in plan[0]["new_text"]


def test_a_splice_that_cannot_land_is_refused() -> None:
    # The same backstop as the block-list case above, reached a second way. A
    # ConfigMap the broker cannot read makes the pool keep its previous
    # configuration and drop nothing, and the only place that shows up is the
    # broker log. So refuse to write it.
    config = {"drop_task_killswitch": ["a.b"], "demoted_namespaces": None}
    text = "drop_task_killswitch:\n# why\n- a.b\ndemoted_namespaces:\n"
    with pytest.raises(SystemExit) as e:
        _plan([_entry("default", config, text)], "add", "sentry.tasks.unmerge")
    assert "not valid YAML" in str(e.value)


def test_a_config_that_disagrees_with_the_live_text_is_refused() -> None:
    # `config` and `text` both come from the read step, so they cannot disagree
    # unless something upstream is broken. Catch it here rather than writing a
    # pool a later step can no longer explain.
    config = {"drop_task_killswitch": None, "demoted_namespaces": ["ns1"]}
    with pytest.raises(SystemExit) as e:
        _plan([_entry("default", config, BARE)], "add", "sentry.tasks.unmerge")
    assert "did not round-trip" in str(e.value)


def test_a_report_entry_with_no_config_is_a_workflow_bug_not_an_operator_one() -> None:
    entry = _bare_entry("default")
    entry["config"] = None
    with pytest.raises(SystemExit) as e:
        _plan([entry], "add", "sentry.tasks.unmerge")
    assert "bug in this workflow" in str(e.value)


def test_the_plan_takes_the_report_as_hera_hands_it_over() -> None:
    # Hera's preamble parses every parameter as JSON, so the report arrives as a
    # list of dicts rather than as the text the previous step wrote.
    killswitch.plan_change(
        [_bare_entry("default")],  # type: ignore[arg-type]
        "add",
        "sentry.tasks.unmerge",
    )
    with open("/tmp/plan.json") as f:
        assert json.load(f)[0]["changed"] is True


def test_the_plan_carries_the_rule_list_beside_the_text() -> None:
    # The followup-PR step prints this list rather than parsing the spliced YAML
    # again, so it has to be the same list the splice wrote.
    plan = _plan([_bare_entry("default", ["already.there"])], "add", "a.b")
    assert plan[0]["rules"] == ["already.there", "a.b"]
    emptied = _plan([_bare_entry("default", ["a.b"])], "remove", "a.b")
    assert emptied[0]["rules"] == []


# --- apply_change ----------------------------------------------------------


def _cm(name: str, text: str, resource_version: str = "1") -> mock.MagicMock:
    m = mock.MagicMock()
    m.metadata.name = name
    m.metadata.resource_version = resource_version
    m.data = {"runtime-config": text}
    return m


def _apply(
    plan: list[dict[str, Any]], live: dict[str, mock.MagicMock], namespace: str = NS
) -> mock.MagicMock:
    with (
        mock.patch("kubernetes.config.load_kube_config") as load_cfg,
        mock.patch("kubernetes.client.CoreV1Api") as Core,
    ):
        core = Core.return_value
        core.read_namespaced_config_map.side_effect = lambda name, ns: live[name]
        killswitch.apply_change(json.dumps(plan), namespace)
        load_cfg.assert_called_once()
        return cast(mock.MagicMock, core)


def _planned(pool: str, old: str, new: str, changed: bool = True) -> dict[str, Any]:
    name = f"task-{pool}-broker-runtime-config"
    return {
        "pool": pool,
        "configmap": name,
        "old_text": old,
        "new_text": new,
        # The plan step carries the rule list beside the text it spliced it into,
        # so the followup-PR step never parses the YAML back out again.
        "rules": list((yaml.safe_load(new) or {}).get("drop_task_killswitch") or []),
        "changed": changed,
        "note": "",
    }


def test_apply_patches_only_the_pools_that_change() -> None:
    new = 'drop_task_killswitch: ["a.b"]\ndemoted_namespaces:\n'
    plan = [
        _planned("default", BARE, new),
        _planned("long", BARE, BARE, changed=False),
    ]
    live = {
        "task-default-broker-runtime-config": _cm("task-default-broker-runtime-config", BARE),
        "task-long-broker-runtime-config": _cm("task-long-broker-runtime-config", BARE),
    }
    core = _apply(plan, live)
    assert core.patch_namespaced_config_map.call_count == 1
    name, ns, body = core.patch_namespaced_config_map.call_args[0]
    assert name == "task-default-broker-runtime-config"
    assert ns == NS
    assert body["data"]["runtime-config"] == new
    # The API server rejects the write if the object moved between the read above
    # and the patch. The text check covers the long window, this the short one.
    assert body["metadata"]["resourceVersion"] == "1"


def test_apply_refuses_when_the_pool_changed_since_the_plan() -> None:
    # Somebody else patched the same pool while this run waited at the approval
    # pause. Writing over them would revert their change with nothing to show
    # for it, so fail and cost a re-run instead.
    new = 'drop_task_killswitch: ["a.b"]\ndemoted_namespaces:\n'
    live = {
        "task-default-broker-runtime-config": _cm(
            "task-default-broker-runtime-config",
            'drop_task_killswitch: ["c.d"]\ndemoted_namespaces:\n',
            resource_version="9",
        )
    }
    with pytest.raises(SystemExit) as e:
        _apply([_planned("default", BARE, new)], live)
    assert "no longer the one this run planned against" in str(e.value)


def test_apply_refuses_a_configmap_that_is_not_a_broker_runtime_config() -> None:
    plan = [_planned("default", BARE, BARE)]
    plan[0]["configmap"] = "some-other-configmap"
    with pytest.raises(SystemExit) as e:
        _apply(plan, {})
    assert "refusing to patch" in str(e.value)


def test_apply_writes_nothing_when_the_plan_changes_nothing() -> None:
    core = _apply([_planned("default", BARE, BARE, changed=False)], {})
    core.patch_namespaced_config_map.assert_not_called()
    core.read_namespaced_config_map.assert_not_called()


# --- read_back -------------------------------------------------------------


def _read_back(
    plan: list[dict[str, Any]],
    live: dict[str, mock.MagicMock],
    region: str = "us",
    task: str = "a.b",
) -> mock.MagicMock:
    with (
        mock.patch("kubernetes.config.load_kube_config"),
        mock.patch("kubernetes.client.CoreV1Api") as Core,
    ):
        core = Core.return_value
        core.read_namespaced_config_map.side_effect = lambda name, ns: live[name]
        killswitch.read_back(json.dumps(plan), NS, region, task)
        return cast(mock.MagicMock, core)


def test_read_back_passes_when_the_pool_holds_what_was_written() -> None:
    new = 'drop_task_killswitch: ["a.b"]\ndemoted_namespaces:\n'
    live = {"task-default-broker-runtime-config": _cm("task-default-broker-runtime-config", new)}
    _read_back([_planned("default", BARE, new)], live)


def test_read_back_fails_when_the_pool_does_not() -> None:
    # Printing the mismatch is not enough: a workflow that goes green on a
    # ConfigMap that does not hold the change is worse than no read-back at all.
    new = 'drop_task_killswitch: ["a.b"]\ndemoted_namespaces:\n'
    live = {"task-default-broker-runtime-config": _cm("task-default-broker-runtime-config", BARE)}
    with pytest.raises(SystemExit) as e:
        _read_back([_planned("default", BARE, new)], live)
    assert "do not hold what this run wrote" in str(e.value)


def test_read_back_prints_the_drop_metric_link_scoped_to_the_region_and_task(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The read-back proves what the ConfigMap holds. Only the metric proves that
    # taskbroker accepted it, so this link is the last word of the workflow.
    new = 'drop_task_killswitch: ["sentry.tasks.unmerge"]\ndemoted_namespaces:\n'
    live = {"task-default-broker-runtime-config": _cm("task-default-broker-runtime-config", new)}
    _read_back([_planned("default", BARE, new)], live, region="us", task="sentry.tasks.unmerge")
    out = capsys.readouterr().out
    assert "taskbroker.filter.drop_task_killswitch" in out
    assert "sentry_region%3Aus%2Ctaskname%3Asentry.tasks.unmerge" in out


# --- followup_pr_instructions ----------------------------------------------
#
# The ops repo change is the durable half. The live patch is reverted by the
# next taskbroker pipeline run unless master carries the same value, so this
# step has to say plainly what to change, and has to say nothing at all when the
# run wrote nothing.


def _followup(plan: list[dict[str, Any]], applied: object, region: str = "us") -> None:
    applied_json = applied if isinstance(applied, str) else json.dumps(applied)
    killswitch.followup_pr_instructions(json.dumps(plan), applied_json, region)


def test_the_printed_rule_list_is_the_pools_whole_list(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # sentry-kube merges mappings and replaces lists, so committing only the rule
    # this run added would delete every other rule the pool holds at the next
    # render. What the step prints is what goes in the file, so it prints all of
    # them.
    old = 'drop_task_killswitch: ["already.there"]\ndemoted_namespaces:\n'
    new = 'drop_task_killswitch: ["already.there", "a.b"]\ndemoted_namespaces:\n'
    _followup([_planned("default", old, new)], ["task-default-broker-runtime-config"])
    out = capsys.readouterr().out
    # In the order the pool holds them, which is the order the file has to carry.
    assert 'default: ["already.there", "a.b"]' in out


def test_the_step_names_the_region_file_and_the_declared_pool_key(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # `us` declares `process-segments` with `shard: 4`, so the cluster holds
    # `process-segments-0` and the override file holds `process-segments`.
    # Writing the cluster's name into the override would create a pool that does
    # not exist and leave the real one untouched, so the instructions have to say
    # which of the two names the file takes.
    new = 'drop_task_killswitch: ["a.b"]\ndemoted_namespaces:\n'
    _followup(
        [_planned("process-segments-0", BARE, new)],
        ["task-process-segments-0-broker-runtime-config"],
    )
    out = capsys.readouterr().out
    assert "k8s/services/taskbroker/region_overrides/us/default.yaml" in out
    assert "DECLARES" in out
    assert "process-segments-0` through `-3" in out


def test_the_shard_warning_is_printed(capsys: pytest.CaptureFixture[str]) -> None:
    # One override key covers every shard it renders, so what gets committed
    # replaces the rule list of shards this run never read. Nothing else in the
    # run says so.
    new = 'drop_task_killswitch: ["a.b"]\ndemoted_namespaces:\n'
    _followup(
        [_planned("process-segments-0", BARE, new)],
        ["task-process-segments-0-broker-runtime-config"],
    )
    out = capsys.readouterr().out
    assert "One key sets every shard it renders" in out
    assert "`action: list` run first" in out


def test_a_removal_that_empties_a_pool_asks_for_the_key_to_be_deleted(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The template writes `drop_task_killswitch: []` when `runtime_config` is
    # defined and the bare key when it is not, and every committed manifest
    # holds the bare key. Committing an empty list would leave a one-line
    # difference the drift job reports three times a day for ever.
    old = 'drop_task_killswitch: ["a.b"]\ndemoted_namespaces:\n'
    _followup([_planned("default", old, BARE)], ["task-default-broker-runtime-config"])
    out = capsys.readouterr().out
    assert "default: []" in out
    assert "delete the whole `runtime_config` block" in out
    assert "Do not commit `drop_task_killswitch: []`" in out


def test_an_aborted_run_asks_for_no_change(capsys: pytest.CaptureFixture[str]) -> None:
    # The apply step is skipped when the approver aborts, and Argo leaves a
    # skipped step's output un-substituted. An instruction printed there is an
    # instruction somebody follows for a change that never happened.
    new = 'drop_task_killswitch: ["a.b"]\ndemoted_namespaces:\n'
    _followup(
        [_planned("default", BARE, new)],
        "{{steps.apply-killswitch-change-3.outputs.parameters.applied}}",
    )
    out = capsys.readouterr().out
    assert "region override file" not in out
    assert "the apply step did not run" in out


def test_a_run_that_patched_nothing_asks_for_no_change(capsys: pytest.CaptureFixture[str]) -> None:
    _followup([_planned("default", BARE, BARE, changed=False)], [])
    out = capsys.readouterr().out
    assert "region override file" not in out
    assert "patched no ConfigMap" in out


def test_only_the_pools_that_were_patched_are_printed(capsys: pytest.CaptureFixture[str]) -> None:
    # A pool that already held the rule is not written, so it has no place in a
    # pull request about this run.
    new = 'drop_task_killswitch: ["a.b"]\ndemoted_namespaces:\n'
    plan = [
        _planned("default", BARE, new),
        _planned("internal", new, new, changed=False),
    ]
    _followup(plan, ["task-default-broker-runtime-config"])
    out = capsys.readouterr().out
    assert "pools patched by this run: ['default']" in out
    assert "internal:" not in out
