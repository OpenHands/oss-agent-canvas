import json
import time
from types import SimpleNamespace

import pytest

from budget import RunBudget
from common import write_json
import parallel


@pytest.mark.parametrize("count,concurrency", [(1, 3), (3, 2), (10, 10)])
def test_topic_limit_counts_dispatches_not_prs_or_successes(
    tmp_path, monkeypatch, count, concurrency
):
    state = {"events": {}, "backlog": {}, "topic_weeks": {}}
    state_path = tmp_path / "state.json"
    context = {
        "output": str(tmp_path),
        "state_path": str(state_path),
        "week": "2026-W44",
    }
    queue = [
        {"id": str(n), "priority": n // 5, "representative_event_ids": [f"event-{n}"]}
        for n in range(15)
    ]
    started, running, peaks = [], [], []

    def launch(config, ctx, topic, budget, root):
        root.mkdir(parents=True)
        assert (
            json.loads(state_path.read_text())["topic_weeks"][
                "2026-W44/" + topic["id"]
            ]["status"]
            == "dispatched"
        )
        n = int(topic["id"])
        resources = (
            [
                {
                    "kind": "pull",
                    "state": "open",
                    "url": f"https://github.com/Example/repo/pull/{n * 10 + i}",
                }
                for i in range(5)
            ]
            if n
            else []
        )
        write_json(
            root / "result.json",
            {
                "outcome": {
                    "topic": topic,
                    "status": "reviewed" if n else "failed",
                    "resources": resources,
                },
                "budget": {"reported_inference_cost_usd": 0.01},
            },
        )
        started.append(n)
        running.append(n)
        peaks.append(len(running))
        polls = 0

        def poll():
            nonlocal polls
            polls += 1
            if polls < 2:
                return None
            if n in running:
                running.remove(n)
            return 0

        return {
            "process": SimpleNamespace(pid=100 + n, poll=poll),
            "root": root,
            "topic": topic,
            "started": time.monotonic(),
        }

    monkeypatch.setattr(parallel, "launch", launch)
    monkeypatch.setattr(parallel.time, "sleep", lambda _: None)
    receipt, updated = parallel.execute(
        {"max_topics_per_run": count, "repair_concurrency": concurrency},
        context,
        queue,
        state,
        RunBudget({"max_inference_cost_usd": 100}),
    )
    assert started == list(range(count))
    assert max(peaks) == min(count, concurrency)
    assert len(
        [r for o in receipt["outcomes"] for r in o.get("resources", [])]
    ) == 5 * (count - 1)
    assert (
        sum(o["status"] == "deferred_topic_limit" for o in receipt["outcomes"])
        == 15 - count
    )
    assert updated["topic_weeks"]["2026-W44/0"]["status"] == "failed"
    assert "event-0" not in updated["events"]
    if count > 1:
        assert len(updated["events"]["event-1"]["pr_urls"]) == 5


def test_launch_failure_does_not_start_a_replacement_topic(tmp_path, monkeypatch):
    state = {"events": {}, "backlog": {}, "topic_weeks": {}}
    queue = [{"id": str(n), "representative_event_ids": []} for n in range(2)]

    def fail(*args):
        raise OSError("unable to start process")

    monkeypatch.setattr(parallel, "launch", fail)
    receipt, _ = parallel.execute(
        {"max_topics_per_run": 1},
        {
            "output": str(tmp_path),
            "state_path": str(tmp_path / "state.json"),
            "week": "2026-W44",
        },
        queue,
        state,
        RunBudget({}),
    )
    assert [o["status"] for o in receipt["outcomes"]] == [
        "deferred_topic_limit",
        "failed",
    ]
    assert len(state["topic_weeks"]) == 1


def test_worker_exception_keeps_partial_remote_work_visible_and_draft(
    tmp_path, monkeypatch
):
    import weekly
    import topic_agent

    request = tmp_path / "request.json"
    write_json(
        request, {"config": {}, "context": {}, "topic": {"id": "one"}, "budget": {}}
    )
    write_json(tmp_path / "context.json", {"topic": {"id": "one"}})
    monkeypatch.setattr(weekly, "prepare_topic", lambda *a: {})

    def fail(*a):
        raise RuntimeError("failure after PR creation")

    monkeypatch.setattr(topic_agent, "run", fail)
    resource = {
        "kind": "pull",
        "state": "open",
        "draft": False,
        "url": "https://github.com/Example/sdk/pull/2",
    }
    monkeypatch.setattr(
        topic_agent, "discover", lambda *a: {"resources": [resource], "errors": []}
    )
    calls = []
    monkeypatch.setattr(topic_agent, "command", lambda argv: calls.append(argv) or "")
    parallel.worker(request)
    result = json.loads((tmp_path / "result.json").read_text())["outcome"]
    assert result["status"] == "review_incomplete"
    assert result["resources"][0]["draft"]
    assert "--undo" in calls[0]
