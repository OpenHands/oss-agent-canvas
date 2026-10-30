import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from budget import BudgetExhausted, RunBudget
from common import read_json, redact, replace_json, write_json


def worker(request_path):
    from weekly import prepare_topic
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
        from topic_agent import discover, hold_drafts

        recovered = {"resources": [], "errors": []}
        if (root / "context.json").exists():
            try:
                recovered = discover(read_json(root / "context.json", 32_000_000), root)
            except Exception as recovery_error:
                recovered["errors"].append(
                    {"error": redact(str(recovery_error))[:1000]}
                )
        recovered["errors"] += hold_drafts(recovered["resources"])
        result["outcome"] = {
            "topic": request["topic"],
            "status": "review_incomplete" if recovered["resources"] else "failed",
            "error": redact(str(error))[:2000],
            **recovered,
        }
    finally:
        result["budget"] = budget.snapshot()
        write_json(root / "result.json", result)


def launch(config, context, topic, budget, root):
    root.mkdir(parents=True, mode=0o700)
    path = root / "request.json"
    write_json(
        path,
        {
            "config": config,
            "context": context,
            "topic": topic,
            "budget": {
                "max_inference_cost_usd": max(0.001, budget.maximum_cost - budget.cost),
                "max_run_seconds": budget.timeout(config.get("max_run_seconds", 10800)),
            },
        },
    )
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
        {"pid": process.pid, "topic_id": topic["id"], "started_at": time.time()},
    )
    return {
        "process": process,
        "root": root,
        "topic": topic,
        "started": time.monotonic(),
        "deadline": time.monotonic() + read_json(path)["budget"]["max_run_seconds"],
    }


def execute(config, context, queue, state, budget):
    from weekly import save_state

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

    def record(topic, outcome, root):
        key = context["week"] + "/" + topic["id"]
        resources = outcome.get("resources", [])
        state["topic_weeks"][key] = {
            "topic": topic,
            "status": outcome["status"],
            "report": str(root / "topic-report.json"),
            "reason": outcome.get("error"),
            "resources": resources,
        }
        prs = [
            r["url"] for r in resources if r["kind"] == "pull" and r["state"] == "open"
        ]
        if prs:
            for identifier in topic["representative_event_ids"]:
                state["events"][identifier] = {"topic_id": topic["id"], "pr_urls": prs}
                state["backlog"].pop(identifier, None)
        replace_json(root / "topic-report.json", outcome)
        save_state(state_path, state)
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
                key = context["week"] + "/" + topic["id"]
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
                    )
                    continue
                active.append(job)
                starts.append(
                    {
                        "topic_id": topic["id"],
                        "priority": topic["priority"],
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
                from topic_agent import discover, hold_drafts

                try:
                    prepared = read_json(root / "context.json", 32_000_000)
                    recovered = discover(prepared, root)
                except Exception as error:
                    recovered = {
                        "resources": [],
                        "errors": [{"error": redact(str(error))[:2000]}],
                    }
                recovered["errors"] += hold_drafts(recovered["resources"])
                outcome = {
                    "topic": topic,
                    "status": "review_incomplete",
                    "error": "Worker exited without a result; remote work requires review",
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
    worker(Path(sys.argv[1]))
