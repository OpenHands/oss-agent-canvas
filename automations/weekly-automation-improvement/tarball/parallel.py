import fcntl
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from budget import BudgetExhausted, RunBudget
from common import read_json, redact, write_json


def reserve_shared(path, key, slot):
    from weekly import save_state

    with Path(str(path) + ".publication.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = read_json(path, 32_000_000)
        current = state["topic_weeks"].get(key, {})
        if (
            current.get("status") in ("reserved", "completed")
            and current.get("report") != slot["report"]
        ):
            raise ValueError("Another attempt owns this topic's publication")
        state["topic_weeks"][key] = slot
        save_state(path, state)


def merge_state(path, initial, updated, topic, week, topic_output):
    from weekly import save_state

    with Path(str(path) + ".publication.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = read_json(path, 32_000_000)
        key = week + "/" + topic["automation_id"] + "/" + topic["id"]
        current = state["topic_weeks"].get(key, {})
        another_owner = (
            current.get("status") in ("reserved", "completed")
            and current.get("report")
            and not Path(current["report"]).is_relative_to(topic_output)
        )
        if not another_owner and updated["topic_weeks"].get(key) != initial[
            "topic_weeks"
        ].get(key):
            if key in updated["topic_weeks"]:
                state["topic_weeks"][key] = updated["topic_weeks"][key]
        for identifier in topic["representative_event_ids"]:
            if (
                updated["events"].get(identifier) != initial["events"].get(identifier)
                and identifier in updated["events"]
            ):
                state["events"][identifier] = updated["events"][identifier]
                state["backlog"].pop(identifier, None)
        save_state(path, state)
    return state


def created_prs(outcomes):
    return sum(
        1
        for outcome in outcomes
        if isinstance(outcome.get("publication"), dict)
        and outcome["publication"].get("pr_url")
        and outcome["publication"].get(
            "created", outcome["publication"].get("status") != "already_tracked"
        )
    )


def worker(request_path):
    from weekly import investigate

    request = read_json(request_path, 32_000_000)
    root = request_path.parent
    state = request["state"]
    write_json(root / "state.json", state)
    budget = RunBudget(request["budget"])
    result = {}
    try:
        topic = request["topic"]
        context = request["context"]
        result["outcome"] = investigate(
            request["config"],
            context["inventory"],
            context["records"],
            context["prs"],
            Path(context["output"]),
            state,
            root / "state.json",
            context["week"],
            context["dry_run"],
            context["limit"],
            context["seed"],
            budget,
            planned=([topic], request["aggregation"]),
        )
    except BaseException as error:
        result["error"] = redact(str(error))[:2000]
    finally:
        result["budget"] = budget.snapshot()
        write_json(root / "result.json", result)


def launch(config, context, topic, aggregation, state, budget, root):
    root.mkdir(parents=True, mode=0o700)
    request = {
        "config": config,
        "context": context,
        "topic": topic,
        "aggregation": aggregation,
        "state": state,
        "budget": {
            "max_inference_cost_usd": max(0.001, budget.maximum_cost - budget.cost),
            "max_run_seconds": budget.timeout(config.get("max_run_seconds", 10800)),
        },
    }
    path = root / "request.json"
    write_json(path, request)
    with (root / "worker.log").open("x") as log:
        (root / "worker.log").chmod(0o600)
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), str(path)],
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    write_json(
        root / "process.json",
        {
            "pid": process.pid,
            "topic_id": topic["id"],
            "priority": topic["priority"],
            "started_at": time.time(),
        },
    )
    return {"process": process, "root": root, "topic": topic, "initial": state}


def execute(config, context, queue, aggregation, state, budget):
    from weekly import source_revision, reuse_topic

    output = Path(context["output"])
    state_path = Path(context["state_path"])
    config = config | {"_source_revision": source_revision(config)}
    if not context["dry_run"]:
        config["_canonical_state_path"] = str(state_path)
    pr_limit = config.get("max_prs_per_run", 10)
    concurrency = config.get("repair_concurrency", pr_limit)
    pending, active, starts, seen = list(queue[:pr_limit]), [], [], set()
    outcomes = [
        {"topic": topic, "status": "deferred_pr_limit"} for topic in queue[pr_limit:]
    ]
    stopped = None
    try:
        while pending or active:
            while pending and len(active) < concurrency and stopped is None:
                try:
                    budget.check()
                except BudgetExhausted as error:
                    stopped = str(error)
                    break
                topic = pending.pop(0)
                identifier = topic["automation_id"]
                previous = [
                    v["topic"]
                    for key, v in state["topic_weeks"].items()
                    if key.split("/")[1] == identifier
                    and v["topic"]["id"] in config.get("retry_topic_ids", [])
                ]
                topic = reuse_topic(topic, previous)
                if topic["id"] in seen:
                    outcomes.append(
                        {"topic": topic, "status": "topic_already_completed_or_tracked"}
                    )
                    continue
                seen.add(topic["id"])
                records = context["grouped"][identifier]
                scoped = {
                    k: v
                    for k, v in aggregation.items()
                    if k
                    not in (
                        "automations",
                        "event_catalog",
                        "groups",
                        "previous_attempts",
                        "luna_triage",
                    )
                }
                scoped.update(
                    automation_id=identifier,
                    total_events=len(records),
                    affected_conversations=len(
                        {r["owner"]["conversation_id"] for r in records}
                    ),
                    groups=[
                        g
                        for g in aggregation["groups"]
                        if g.get("automation_id", identifier) == identifier
                    ],
                    previous_attempts=[
                        t
                        for t in context["previous"]
                        if t["automation_id"] == identifier
                    ],
                    luna_triage={"topics": [topic], "no_action_reason": ""},
                )
                directory = output / identifier
                directory.mkdir(mode=0o700, exist_ok=True)
                task_context = {
                    k: context[k]
                    for k in ("inventory", "prs", "week", "dry_run", "limit", "seed")
                }
                task_context.update(records=records, output=str(directory))
                job = launch(
                    config,
                    task_context,
                    topic,
                    scoped,
                    state,
                    budget,
                    output / "workers" / topic["id"],
                )
                active.append(job)
                starts.append(
                    {
                        "topic_id": topic["id"],
                        "automation_id": identifier,
                        "priority": topic["priority"],
                        "pid": job["process"].pid,
                    }
                )
                write_json(
                    output / f"parallel-start-{len(starts):04d}.json", starts[-1]
                )
            finished = next(
                (job for job in active if job["process"].poll() is not None), None
            )
            if finished is None:
                if active:
                    time.sleep(0.5)
                    continue
                break
            active.remove(finished)
            root, topic = finished["root"], finished["topic"]
            result = (
                read_json(root / "result.json", 32_000_000)
                if (root / "result.json").exists()
                else {"error": "Repair worker exited without its result"}
            )
            accounting = result.get("budget", {})
            budget.record(
                None
                if accounting.get("unknown_cost_requests") or not accounting
                else accounting["reported_inference_cost_usd"]
            )
            if not context["dry_run"] and (root / "state.json").exists():
                state = merge_state(
                    state_path,
                    finished["initial"],
                    read_json(root / "state.json", 32_000_000),
                    topic,
                    context["week"],
                    output / topic["automation_id"] / "topics" / topic["id"],
                )
            if "outcome" in result:
                outcomes.extend(result["outcome"]["topics"])
            else:
                outcomes.append(
                    {
                        "topic": topic,
                        "status": "failed",
                        "error": result.get("error", "Repair worker failed"),
                    }
                )
        outcomes.extend(
            {
                "topic": topic,
                "status": "deferred_pr_limit"
                if stopped == "pr_limit_reached"
                else "deferred_budget",
                "reason": stopped,
            }
            for topic in pending
        )
    finally:
        for job in active:
            if job["process"].poll() is None:
                try:
                    os.killpg(job["process"].pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
    receipt = {
        "concurrency": concurrency,
        "max_prs_per_run": pr_limit,
        "created_prs": created_prs(outcomes),
        "started": starts,
        "outcomes": outcomes,
        "stop_reason": stopped,
        "budget": budget.snapshot(),
    }
    write_json(output / "parallel-repairs.json", receipt)
    return receipt, state


if __name__ == "__main__":
    worker(Path(sys.argv[1]))
