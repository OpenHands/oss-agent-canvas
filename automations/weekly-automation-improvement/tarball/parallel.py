import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from budget import BudgetExhausted, RunBudget
from common import read_json, redact, replace_json, write_json
from repair_policy import permits, resolve as repair_policy, resource_errors


def recover_resources(root, policy):
    from topic_agent import discover, hold_drafts

    recovered = {"resources": [], "errors": []}
    if (root / "context.json").exists():
        try:
            recovered = discover(read_json(root / "context.json", 32_000_000), root)
        except Exception as error:
            recovered["errors"].append({"error": redact(str(error))[:1000]})
    recovered["errors"] += [
        {"error": error} for error in resource_errors(policy, recovered["resources"])
    ]
    recovered["errors"] += hold_drafts(
        [r for r in recovered["resources"] if permits(policy, r["repository"])]
    )
    return recovered


def worker(request_path):
    from repair_workflow import prepare_topic
    from topic_agent import run

    request = read_json(request_path, 32_000_000)
    root = request_path.parent
    budget = RunBudget(request["budget"])
    result = {}
    try:
        context = prepare_topic(
            request["config"], request["context"], request["topic"], root
        )
        result["outcome"] = run(request["config"], context, root, budget)
    except Exception as error:
        recovered = recover_resources(root, repair_policy(request["config"]))
        result["outcome"] = {
            "topic": request["topic"],
            "status": "review_incomplete" if recovered["resources"] else "failed",
            "error": redact(str(error))[:2000],
            "resource_recovery_pending": bool(recovered["errors"]),
            **recovered,
        }
    finally:
        result["budget"] = budget.snapshot()
        replace_json(root / "result.json", result)


def launch(config, context, topic, budget, root):
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    lock = config.get("_workflow_lock_fd")
    path = root / "request.json"
    seconds = budget.timeout(config.get("max_run_seconds", 10800))
    write_json(
        path,
        {
            "config": {k: v for k, v in config.items() if k != "_workflow_lock_fd"},
            "context": context,
            "topic": topic,
            "budget": {
                "max_inference_cost_usd": max(0.001, budget.maximum_cost - budget.cost),
                "max_run_seconds": seconds,
            },
        },
    )
    with (root / "worker.log").open("x") as log:
        (root / "worker.log").chmod(0o600)
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), str(path)]
            + ([str(lock)] if lock is not None else []),
            pass_fds=(lock,) if lock is not None else (),
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    started = time.monotonic()
    return {
        "process": process,
        "root": root,
        "topic": topic,
        "started": started,
        "deadline": started + seconds,
    }


def ledger_key(context, topic):
    run_id = Path(context["output"]).name if topic.get("issue_url") else context["week"]
    return run_id + "/" + topic["id"]


def record_outcome(config, context, state, topic, outcome, root):
    resources = outcome.get("resources", [])
    state["topic_weeks"][ledger_key(context, topic)] = {
        "topic": topic,
        "status": outcome["status"],
        "report": str(root / "topic-report.json"),
        "reason": outcome.get("error"),
        "resources": resources,
        "decision": (outcome.get("review_decision") or {}).get("decision"),
    }
    prs = [
        r["url"]
        for r in resources
        if r["kind"] == "pull"
        and r["state"] == "open"
        and permits(repair_policy(config), r["repository"])
    ]
    if prs:
        for identifier in topic["representative_event_ids"]:
            state["events"].setdefault(identifier, {}).update(
                topic_id=topic["id"], pr_urls=prs
            )
            state["backlog"].pop(identifier, None)
    replace_json(root / "topic-report.json", outcome)
    replace_json(Path(context["state_path"]), state)


def reconcile_claims(config, context, state):
    from backlog_dispatch import finish

    reconciled = []
    for path in sorted((Path(context["state_dir"]) / "native-claims").glob("*.json")):
        topic = {"id": path.stem}
        try:
            claim = read_json(path)
            topic = claim["topic"]
            previous = context | {"output": claim["output"]}
            root = Path(previous["output"]) / "workers" / topic["id"]
            result_path = root / "result.json"
            report_path = root / "topic-report.json"
            if result_path.exists():
                outcome = read_json(result_path, 32_000_000)["outcome"]
            else:
                outcome = (
                    read_json(report_path, 32_000_000) if report_path.exists() else {}
                )
                if not outcome.get("attempts") and (root / "context.json").exists():
                    outcome = {"resource_recovery_pending": True}
                if not outcome or outcome.get("resource_recovery_pending"):
                    outcome = {
                        "topic": topic,
                        "status": "review_incomplete",
                        "error": "The prior attempt ended without a final worker receipt",
                        **outcome,
                    }
            if outcome.get("resource_recovery_pending"):
                recovered = recover_resources(root, repair_policy(config))
                outcome |= recovered | {
                    "resource_recovery_pending": bool(recovered["errors"])
                }
            record_outcome(config, previous, state, topic, outcome, root)
            finish(previous, topic, outcome, root)
            reconciled.append(
                outcome
                | {"reconciled": not outcome.get("resource_recovery_pending", False)}
            )
        except Exception as error:
            reconciled.append(
                {
                    "topic": topic,
                    "status": "review_incomplete",
                    "claim_release_error": redact(str(error))[:1000],
                }
            )
    return reconciled


def execute(config, context, queue, state, budget):
    from backlog_dispatch import claim, finish
    from triage_workflow import save_state

    output, state_path = Path(context["output"]), Path(context["state_path"])
    count = config.get("max_topics_per_run", 10)
    concurrency = min(config.get("repair_concurrency", count), count)
    selected, seen = [], set()
    for topic in queue:
        if topic["id"] not in seen:
            selected.append(topic)
            seen.add(topic["id"])
    pending, active, starts = list(selected[:count]), [], []
    outcomes = [
        {"topic": t, "status": "deferred_topic_limit"} for t in selected[count:]
    ]
    stopped = None

    def record(topic, outcome, root, *, claimed=True):
        record_outcome(config, context, state, topic, outcome, root)
        if claimed and topic.get("issue_url"):
            try:
                finish(context, topic, outcome, root)
            except Exception as error:
                outcome = outcome | {
                    "status": "review_incomplete",
                    "claim_release_error": redact(str(error))[:1000],
                }
        outcomes.append(outcome)

    try:
        while pending or active:
            while pending and len(active) < concurrency and stopped is None:
                try:
                    budget.check()
                except BudgetExhausted as error:
                    stopped = str(error)
                    break
                topic = pending.pop(0)
                root = output / "workers" / topic["id"]
                claimed = False
                if topic.get("issue_url"):
                    try:
                        claimed = claim(config, context, topic, root, state)
                    except Exception as error:
                        root.mkdir(parents=True, mode=0o700, exist_ok=True)
                        record(
                            topic,
                            {
                                "topic": topic,
                                "status": "review_incomplete",
                                "error": redact(str(error))[:2000],
                            },
                            root,
                            claimed=False,
                        )
                        continue
                    if not claimed:
                        outcomes.append({"topic": topic, "status": "issue_changed"})
                        continue
                key = ledger_key(context, topic)
                state["topic_weeks"][key] = {
                    "topic": topic,
                    "status": "dispatched",
                    "report": str(root / "topic-report.json"),
                }
                save_state(state_path, state)
                try:
                    job = launch(config, context, topic, budget, root)
                except Exception as error:
                    root.mkdir(parents=True, mode=0o700, exist_ok=True)
                    record(
                        topic,
                        {
                            "topic": topic,
                            "status": "failed",
                            "error": redact(str(error))[:2000],
                        },
                        root,
                        claimed=claimed,
                    )
                    continue
                active.append(job)
                write_json(
                    root / "process.json",
                    {
                        "pid": job["process"].pid,
                        "topic_id": topic["id"],
                        "started_at": time.time(),
                    },
                )
                starts.append(
                    {
                        "topic_id": topic["id"],
                        "priority": topic["priority"],
                        "issue_url": topic.get("issue_url"),
                        "pid": job["process"].pid,
                    }
                )
                write_json(output / f"topic-start-{len(starts):04d}.json", starts[-1])
            finished = next(
                (j for j in active if j["process"].poll() is not None), None
            )
            if finished is None:
                for job in active:
                    deadline = job.get(
                        "deadline",
                        job["started"] + config.get("max_run_seconds", 10800),
                    )
                    overdue = time.monotonic() - deadline
                    if overdue > 60:
                        try:
                            os.killpg(
                                job["process"].pid,
                                signal.SIGKILL if overdue > 70 else signal.SIGTERM,
                            )
                        except ProcessLookupError:
                            pass
                if active:
                    time.sleep(0.5)
                    continue
                break
            active.remove(finished)
            root, topic = finished["root"], finished["topic"]
            result = (
                read_json(root / "result.json", 32_000_000)
                if (root / "result.json").exists()
                else {}
            )
            accounting = result.get("budget", {})
            budget.record(
                None
                if not accounting or accounting.get("unknown_cost_requests")
                else accounting["reported_inference_cost_usd"]
            )
            outcome = result.get("outcome")
            if not outcome:
                recovered = recover_resources(root, repair_policy(config))
                outcome = {
                    "topic": topic,
                    "status": "review_incomplete",
                    "error": "Worker exited without a result; remote work requires review",
                    "resource_recovery_pending": bool(recovered["errors"]),
                    **recovered,
                }
            record(topic, outcome, root)
        outcomes.extend(
            {"topic": t, "status": "deferred_budget", "reason": stopped}
            for t in pending
        )
    finally:
        for job in active:
            if job["process"].poll() is None:
                try:
                    os.killpg(job["process"].pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
    status = (
        "partial_failure"
        if any(o["status"] == "failed" for o in outcomes)
        else "review_incomplete"
        if any(o["status"] == "review_incomplete" for o in outcomes)
        else "budget_exhausted"
        if stopped
        else "completed"
    )
    receipt = {
        "status": status,
        "concurrency": concurrency,
        "max_topics_per_run": count,
        "started": starts,
        "outcomes": outcomes,
        "stop_reason": stopped,
        "budget": budget.snapshot(),
    }
    write_json(output / "parallel-repairs.json", receipt)
    return receipt, state


if __name__ == "__main__":
    if len(sys.argv) > 2:
        os.set_inheritable(int(sys.argv[2]), False)
    worker(Path(sys.argv[1]))
