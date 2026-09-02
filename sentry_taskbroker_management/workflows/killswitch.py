"""
The taskbroker killswitch: read, plan, apply, confirm and follow up on the
`drop_task_killswitch` rules a region's broker runtime-config ConfigMaps hold.
Each function here is one step of the ops repo's `taskbroker-killswitch` Argo
workflow. The ops repo wires them into Argo templates; this module holds the
taskbroker-specific half.

Two constraints come from how the ops repo runs these:
* Each function body is inlined as the whole script of a container, so a
  function may only use its own parameters, its own local imports and the
  standard library. Nothing at module level reaches the container.
* Parameter names are the Argo input parameter names, so renaming one is a
  change to the ops repo's workflow, not a local rename.
"""


def list_killswitches(pools: str, region: str, live_pools_json: str, namespace: str) -> None:
    # Read the `task-*-broker-runtime-config` ConfigMap of each selected pool
    import json
    from typing import Any, TypedDict

    import yaml
    from kubernetes import client, config  # type: ignore[import-untyped]
    from kubernetes.client.rest import ApiException  # type: ignore[import-untyped]

    PREFIX = "task-"
    SUFFIX = "-broker-runtime-config"

    class PoolEntry(TypedDict):
        """
        What this step reports for one selected pool.
        """

        pool: str
        configmap: str
        rules: list[str]
        problems: list[str]
        text: str | None
        config: dict[str, Any] | None

    if not isinstance(pools, str):
        raise SystemExit(
            f"pools arrived as a {type(pools).__name__}, because the value parses as JSON. "
            "Write it as plain text: a comma separated list of pool names, or 'all'."
        )

    want_all = pools.strip() == "all"
    selected = [p.strip() for p in pools.split(",") if p.strip()]
    if not want_all and not selected:
        raise SystemExit("pools is required: a comma separated list of pool names, or 'all'.")

    # `all` means every pool the ops repo declares, not every pool the cluster holds
    live_pools = (
        live_pools_json if isinstance(live_pools_json, dict) else json.loads(live_pools_json)
    )
    if region not in live_pools:
        raise SystemExit(
            f"region '{region}' has no taskbroker config in the ops repo "
            f"(known regions: {sorted(live_pools)})."
        )
    region_pools = live_pools[region]

    if want_all:
        wanted = sorted(region_pools)
    else:
        unknown = sorted({p for p in selected if p not in region_pools})
        if unknown:
            raise SystemExit(
                f"pool(s) {unknown} are not declared for region '{region}' in the ops "
                f"repo. Pools in '{region}': {sorted(region_pools)}."
            )
        wanted = sorted(set(selected))

    def shape_problem(parsed: object) -> str:
        # This is a shape check, not a parser
        if not isinstance(parsed, dict):
            return f"`runtime-config` is a {type(parsed).__name__}, expected a mapping"
        if "drop_task_killswitch" not in parsed:
            return (
                "`runtime-config` has no `drop_task_killswitch` key, so there is no "
                "line for a change to replace"
            )
        rules = parsed["drop_task_killswitch"]
        if rules is None:
            return ""
        if not isinstance(rules, list):
            return f"`drop_task_killswitch` is a {type(rules).__name__}, expected a list"
        odd = [rule for rule in rules if not isinstance(rule, str)]
        if odd:
            return (
                f"`drop_task_killswitch` holds {len(odd)} entry(s) that are not task "
                f"names: {odd!r}. This workflow reads and writes a list of names"
            )
        return ""

    config.load_kube_config()
    core = client.CoreV1Api()

    found = {}
    absent = []
    for pool in wanted:
        try:
            found[pool] = core.read_namespaced_config_map(PREFIX + pool + SUFFIX, namespace)
        except ApiException as err:
            if err.status != 404:
                raise
            absent.append(pool)

    if absent and not want_all:
        raise SystemExit(
            f"no runtime-config ConfigMap for pool(s) {absent} in namespace "
            f"'{namespace}'. The ops repo declares them for region '{region}', so "
            "either the pool has not been created yet or the credentials point at "
            "the wrong cluster."
        )

    if not found:
        raise SystemExit(
            f"none of the {len(wanted)} pool(s) the ops repo declares for region "
            f"'{region}' have a runtime-config ConfigMap in namespace '{namespace}'. "
            "Either the region has no taskbroker, or the credentials point at the "
            "wrong cluster."
        )

    chosen = sorted(found)

    report: list[PoolEntry] = []
    for pool in chosen:
        cm = found[pool]
        entry: PoolEntry = {
            "pool": pool,
            "configmap": cm.metadata.name,
            "rules": [],
            "problems": [],
            "text": None,
            "config": None,
        }
        raw = (cm.data or {}).get("runtime-config")
        if raw is None:
            entry["problems"].append("the ConfigMap has no `runtime-config` key")
            report.append(entry)
            continue
        entry["text"] = raw

        try:
            parsed = yaml.safe_load(raw)
        except yaml.YAMLError as e:
            entry["problems"].append(f"`runtime-config` is not valid YAML: {e}")
            report.append(entry)
            continue

        if parsed is None:
            parsed = {}
        problem = shape_problem(parsed)
        if problem:
            entry["problems"].append(problem)
            report.append(entry)
            continue

        entry["config"] = parsed
        # Both the bare key and an explicit empty list mean "no killswitch".
        entry["rules"] = list(parsed["drop_task_killswitch"] or [])
        report.append(entry)

    with open("/tmp/killswitches.json", "w") as f:
        json.dump(report, f, default=str)

    print(f"namespace={namespace} pools={pools}")
    print(f"{len(found)} broker runtime-config ConfigMap(s) read, " f"{len(chosen)} selected.")
    # Only reachable on `all`: a named pool that is absent already stopped the run above
    if absent:
        print(
            f"{len(absent)} pool(s) the ops repo declares for '{region}' have no "
            f"ConfigMap in the cluster and were skipped: {absent}"
        )
    print("")

    with_rules = [e for e in report if e["rules"]]
    for entry in report:
        print(f"{entry['configmap']}  (pool '{entry['pool']}')")
        for problem in entry["problems"]:
            print(f"    cannot read: {problem}")
        if not entry["rules"] and not entry["problems"]:
            print("    no killswitch rules")
        for rule in entry["rules"]:
            print(f"    {rule}")
        print("")

    print(f"{len(with_rules)} of {len(chosen)} selected pool(s) hold killswitch rules.")

    by_task: dict[str, list[str]] = {}
    for entry in report:
        for rule in entry["rules"]:
            by_task.setdefault(rule, []).append(entry["pool"])
    if by_task:
        print("")
        print("Killswitched task names, and the pools each one is set in:")
        for taskname in sorted(by_task):
            pool_list = sorted(set(by_task[taskname]))
            print(f"  {taskname}  ({len(pool_list)} pool(s)): {', '.join(pool_list)}")

    unreadable = [e for e in report if e["problems"]]
    if unreadable:
        print("")
        print(
            "Nothing was written, because this workflow cannot plan a change "
            "against a runtime-config that is not a list of task names."
        )
        raise SystemExit(
            f"{len(unreadable)} pool(s) hold a runtime-config this workflow cannot "
            f"read: {sorted(e['pool'] for e in unreadable)}. To act on the other "
            "pools while these are being repaired, re-run with `pools` naming the "
            "pools you need instead of 'all'."
        )


def plan_change(killswitches_json: str, action: str, rule_json: str) -> None:
    # Work out what each selected pool would hold in order to produce a diff
    import difflib
    import json
    import re
    from typing import TypedDict

    import yaml

    class PlanEntry(TypedDict):
        """
        What this step plans for one selected pool.
        """

        pool: str
        configmap: str
        old_text: str
        new_text: str
        rules: list[str]
        changed: bool
        note: str

    report = (
        json.loads(killswitches_json) if isinstance(killswitches_json, str) else (killswitches_json)
    )
    if not isinstance(report, list):
        raise SystemExit(f"killswitches must be a JSON array, got a {type(report).__name__}")

    rule = rule_json
    if isinstance(rule, str):
        try:
            rule = json.loads(rule)
        except ValueError:
            pass
    if not isinstance(rule, str):
        raise SystemExit(
            f"rule arrived as a {type(rule).__name__}, not a task name. A rule is one "
            "activation task name, for example "
            "'sentry.replays.tasks.delete_recording_async'. The validate step of the ops "
            "repo already refuses anything else, so this is a bug in the caller rather "
            "than something an operator can fix by re-running."
        )
    if action not in ("add", "remove"):
        raise SystemExit(f"action must be 'add' or 'remove', got {action!r}")

    def splice(old_text: str, rules: list[str], pool: str) -> str:
        # Write the line exactly as the ops repo's template renders it
        new_line = (
            "drop_task_killswitch: " + json.dumps(rules, sort_keys=True)
            if rules
            else "drop_task_killswitch:"
        )
        lines = old_text.splitlines()
        start = next(
            (i for i, line in enumerate(lines) if re.match(r"drop_task_killswitch\s*:", line)),
            None,
        )
        if start is None:
            raise SystemExit(
                f"{pool}: the live runtime-config has no `drop_task_killswitch` line, which "
                "the read step should already have refused. That is a bug in this workflow, "
                "not something an operator can fix."
            )
        spliced = "\n".join(lines[:start] + [new_line] + lines[start + 1 :])
        return spliced + "\n" if old_text.endswith("\n") else spliced

    def check_splice(new_text: str, rules: list[str], config: dict[str, object], pool: str) -> None:
        # Read the spliced text back and confirm it says what was meant
        try:
            parsed = yaml.safe_load(new_text)
        except yaml.YAMLError as e:
            raise SystemExit(f"{pool}: the new runtime-config is not valid YAML: {e}")
        if not isinstance(parsed, dict):
            raise SystemExit(
                f"{pool}: the new runtime-config reads back as a "
                f"{type(parsed).__name__}, expected a mapping. Refusing to write it."
            )
        wanted = dict(config, drop_task_killswitch=list(rules))
        got = dict(
            parsed,
            drop_task_killswitch=list(parsed.get("drop_task_killswitch") or []),
        )
        if got != wanted:
            raise SystemExit(
                f"{pool}: the new runtime-config did not round-trip. Meant to write "
                f"{wanted!r}, the spliced text reads back as {got!r}. Refusing to write "
                "it. Patch this pool by hand with the runbook."
            )

    plan: list[PlanEntry] = []
    changed_pools = []
    for entry in report:
        pool = entry["pool"]
        config = entry.get("config")
        old_text = entry.get("text")
        if not isinstance(config, dict) or not isinstance(old_text, str):
            raise SystemExit(
                f"{pool}: the read step handed over no runtime-config to plan against. "
                "That is a bug in this workflow, not something an operator can fix."
            )

        old_rules = list(config.get("drop_task_killswitch") or [])
        rules = list(old_rules)
        note = ""
        if action == "add":
            if rule in rules:
                note = "the rule is already present, so no rule is added here"
            else:
                rules.append(rule)
        else:
            remaining = [r for r in rules if r != rule]
            if len(remaining) == len(rules):
                # Most pools legitimately have nothing to remove
                note = "no rule equal to this one, so no rule is removed here"
            rules = remaining

        # A pool whose rules this run does not change is not written at all
        changed = rules != old_rules
        new_text = old_text
        if changed:
            new_text = splice(old_text, rules, pool)
            check_splice(new_text, rules, config, pool)
        if changed:
            changed_pools.append(pool)
        plan.append(
            {
                "pool": pool,
                "configmap": entry["configmap"],
                "old_text": old_text,
                "new_text": new_text,
                "rules": rules,
                "changed": changed,
                "note": note,
            }
        )

    print(f"action: {action}")
    print(f"rule:   {rule}")
    print(f"pools:  {len(plan)} selected, {len(changed_pools)} would be patched")
    print("")

    for item in plan:
        print(f"{item['configmap']}  (pool '{item['pool']}')")
        if item["note"]:
            print(f"    {item['note']}")
        if not item["changed"]:
            print("    no change")
        else:
            for line in difflib.unified_diff(
                item["old_text"].splitlines(),
                item["new_text"].splitlines(),
                fromfile=f"{item['configmap']} live",
                tofile=f"{item['configmap']} planned",
                lineterm="",
            ):
                print(f"    {line}")
        print("")

    if action == "remove" and not changed_pools:
        # A remove that matches nothing is the failure worth being loud about
        raise SystemExit(
            f"no selected pool holds a rule equal to {rule}, so there is nothing to "
            "remove. A removal matches the task name exactly, so check the spelling "
            "against an `action: list` run."
        )

    with open("/tmp/plan.json", "w") as f:
        json.dump(plan, f, default=str)
    with open("/tmp/changed.txt", "w") as f:
        f.write("true" if changed_pools else "false")

    if changed_pools:
        print(f"{len(changed_pools)} pool(s) would be patched: {sorted(changed_pools)}")
        print("")
        print(
            "Read the diff above, then click Resume if you are confirming the "
            "killswitch should be applied."
        )
        print(
            "Activations dropped by a killswitch are deleted. They are not queued, not "
            "retried and not recoverable."
        )
    else:
        print(
            "No pool would change, so there is nothing to approve and nothing to apply. "
            "For an add, that means every selected pool already holds this rule."
        )


def apply_change(plan_json: str, namespace: str) -> None:
    import json

    from kubernetes import client, config  # type: ignore[import-untyped]

    PREFIX = "task-"
    SUFFIX = "-broker-runtime-config"

    plan = json.loads(plan_json) if isinstance(plan_json, str) else plan_json
    if not isinstance(plan, list):
        raise SystemExit(f"plan must be a JSON array, got a {type(plan).__name__}")

    wanted = [item for item in plan if item.get("changed")]
    for item in wanted:
        name = item["configmap"]
        if not (name.startswith(PREFIX) and name.endswith(SUFFIX)):
            raise SystemExit(f"refusing to patch '{name}': not a broker runtime-config")

    config.load_kube_config()
    core = client.CoreV1Api()

    ready = []
    drifted = []
    for item in wanted:
        name = item["configmap"]
        live = core.read_namespaced_config_map(name, namespace)
        live_text = (live.data or {}).get("runtime-config")
        if live_text == item["old_text"]:
            ready.append((item, live.metadata.resource_version))
        else:
            drifted.append(name)

    if drifted:
        raise SystemExit(
            f"{len(drifted)} ConfigMap(s) hold a runtime-config that is no longer the one "
            f"this run planned against: {sorted(drifted)}. Somebody changed them after the "
            "plan was printed. Nothing has been written, to any pool. Re-run the workflow "
            "to plan against what the pools hold now."
        )

    applied: list[str] = []
    try:
        for item, resource_version in ready:
            name = item["configmap"]
            core.patch_namespaced_config_map(
                name,
                namespace,
                {
                    "metadata": {"resourceVersion": resource_version},
                    "data": {"runtime-config": item["new_text"]},
                },
            )
            applied.append(name)
            print(f"{name}: patched")
    except Exception:
        if applied:
            print("")
            print(
                f"{len(applied)} ConfigMap(s) were patched before this failure and hold "
                f"the rule now: {sorted(applied)}. The change is not durable. Name these "
                "pools in the ops repo pull request yourself, or take the rule off them "
                "with a `remove` run. A re-run of this workflow will not name them, "
                "because it plans them as already changed."
            )
        raise

    with open("/tmp/applied.json", "w") as f:
        json.dump(applied, f)

    print("")
    print(f"{len(applied)} ConfigMap(s) patched.")
    if not applied:
        print("Nothing in the plan would change, so nothing was written.")


def read_back(plan_json: str, namespace: str, region: str, task_name: str) -> None:
    import json
    from urllib.parse import quote

    from kubernetes import client, config  # type: ignore[import-untyped]

    plan = json.loads(plan_json) if isinstance(plan_json, str) else plan_json
    if not isinstance(plan, list):
        raise SystemExit(f"plan must be a JSON array, got a {type(plan).__name__}")

    config.load_kube_config()
    core = client.CoreV1Api()

    wrong = []
    checked = 0
    for item in plan:
        if not item.get("changed"):
            continue
        name = item["configmap"]
        live = core.read_namespaced_config_map(name, namespace)
        live_text = (live.data or {}).get("runtime-config")
        checked += 1
        if live_text == item["new_text"]:
            print(f"{name}: holds the planned runtime-config")
        else:
            wrong.append(name)
            print(f"{name}: DOES NOT hold the planned runtime-config")
            print(f"    wanted: {item['new_text']!r}")
            print(f"    live:   {live_text!r}")

    print("")
    print(f"{checked} ConfigMap(s) re-read, {len(wrong)} wrong.")

    scope = quote(f"sentry_region:{region},taskname:{task_name}")
    link = (
        "https://app.datadoghq.com/metric/explorer"
        "?exp_metric=taskbroker.filter.drop_task_killswitch"
        f"&exp_scope={scope}&exp_agg=sum"
    )
    print("")
    print(
        "Whether taskbroker took the change is a different question, and this step "
        "cannot answer it. A broker that refuses a runtime-config keeps the one it "
        "already had and says so only in its own log. The drop metric is the proof:"
    )
    print("")
    print(f"    {link}")
    print("")
    print(
        "Allow about two minutes: the kubelet syncs the file into the pod, then "
        "taskbroker reloads it within 60s. A flat line after that means the task name "
        "is wrong, the task is not flowing, or the broker refused the file. If the "
        "graph is empty even without a task, drop the sentry_region filter first: the "
        "tag value does not have to match this workflow's region name."
    )

    if wrong:
        raise SystemExit(
            f"{len(wrong)} ConfigMap(s) do not hold what this run wrote: {sorted(wrong)}. "
            "Treat the change as not applied and check the pool by hand."
        )


def followup_pr_instructions(plan_json: str, applied_json: str, region: str) -> None:
    import json

    REGION_FILE = "k8s/services/taskbroker/region_overrides/{region}/default.yaml"

    plan = json.loads(plan_json) if isinstance(plan_json, str) else plan_json
    if not isinstance(plan, list):
        raise SystemExit(f"plan must be a JSON array, got a {type(plan).__name__}")

    applied: object = applied_json
    if isinstance(applied, str):
        try:
            applied = json.loads(applied)
        except ValueError:
            applied = None

    def print_instructions(applied: list[str]) -> None:
        # What to change, for a run that really wrote something.
        applied_names = set(applied)
        patched = {
            item["pool"]: list(item.get("rules") or [])
            for item in plan
            if item["configmap"] in applied_names
        }

        print(f"region override file: {REGION_FILE.format(region=region)}")
        print(f"pools patched by this run: {sorted(patched)}")
        print("")
        print(
            "The ConfigMap change is live now, but it is not durable: the next render "
            "of the ops repo replaces it. Open a pull request that makes the same "
            "change in the file above."
        )
        print("")
        print(
            "  1. Find the `processing_pools_dict` key that DECLARES each pool listed "
            "above. A sharded pool is declared once. The cluster holds "
            "`process-segments-0` through `-3`, and the file holds `process-segments`."
        )
        print(
            "  2. Under that key, set `broker.runtime_config.drop_task_killswitch` to "
            "the rule list printed below. Merge into the key the file already has, and "
            "do not add a second copy of it. Everything else under the key stays."
        )
        print(
            "  3. One key sets every shard it renders. If this run patched only some "
            "shards of a pool, what you commit replaces the rule list of the other "
            "shards too. Read them with an `action: list` run first, and commit a list "
            "that is correct for all of them."
        )
        print("")
        print("Rule list each patched pool now holds:")
        for pool in sorted(patched):
            print(f"    {pool}: {json.dumps(patched[pool])}")
        print("")
        print("This pull request represents the durable change.")

    if not isinstance(applied, list):
        print(
            "the apply step did not run, so nothing was written and there is nothing "
            "for the ops repo to change. This is the normal ending for a run the "
            "approver aborted, and for a plan that would change no pool."
        )
    elif not applied:
        print("the apply step patched no ConfigMap, so there is nothing for the ops repo.")
    else:
        print_instructions(applied)
