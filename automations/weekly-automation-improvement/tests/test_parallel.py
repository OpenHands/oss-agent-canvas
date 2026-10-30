import copy
import json
from types import SimpleNamespace

import pytest

from test_weekly import weekly
from budget import RunBudget
from common import write_json
import parallel


def state():
    return {"topic_weeks": {}, "events": {}, "backlog": {}}


def topic(number):
    return {
        "id": str(number),
        "automation_id": "automation",
        "priority": number // 3,
        "representative_event_ids": ["event-" + str(number)],
        "group_ids": ["group-" + str(number)],
    }


@pytest.mark.parametrize("limit,concurrency", [(1, 3), (2, 3), (10, None)])
def test_parallel_queue_dispatches_top_topics_without_replacing_failed_attempts(
    tmp_path, monkeypatch, limit, concurrency
):
    initial = state()
    path = tmp_path / "state.json"
    write_json(path, initial)
    context = {
        "output": str(tmp_path),
        "state_path": str(path),
        "dry_run": False,
        "week": "2026-W44",
        "inventory": {},
        "prs": [],
        "limit": 3,
        "seed": 42,
        "previous": [],
        "grouped": {"automation": [{"owner": {"conversation_id": "one"}}]},
    }
    queue = [topic(n) for n in range(16)]
    started, running, peaks = [], [], []
    monkeypatch.setattr(weekly, "source_revision", lambda c: "a" * 40)
    monkeypatch.setattr(parallel.time, "sleep", lambda seconds: None)

    def launch(config, context, selected, aggregation, ledger, budget, root):
        assert config["_canonical_state_path"] == str(path)
        assert config["_source_revision"] == "a" * 40
        root.mkdir(parents=True)
        number = int(selected["id"])
        outcome = {"topic": selected, "status": "unrepaired"}
        if number == 0:
            outcome["status"] = "failed"
        elif number == 1:
            outcome["publication"] = {
                "status": "already_tracked",
                "created": False,
                "pr_url": "https://github.test/existing",
            }
        else:
            outcome["publication"] = {"created": True, "pr_url": str(number)}
        write_json(
            root / "result.json",
            {
                "outcome": {"topics": [outcome]},
                "budget": {
                    "unknown_cost_requests": 0,
                    "reported_inference_cost_usd": 0.01,
                },
            },
        )
        write_json(root / "state.json", ledger)
        running.append(number)
        peaks.append(len(running))
        started.append(number)
        polls = 0

        def poll():
            nonlocal polls
            polls += 1
            if polls == 1:
                return None
            if number in running:
                running.remove(number)
            return 0

        return {
            "process": SimpleNamespace(pid=100 + number, poll=poll),
            "topic": selected,
            "root": root,
            "initial": ledger,
        }

    monkeypatch.setattr(parallel, "launch", launch)
    config = {"max_prs_per_run": limit}
    if concurrency is not None:
        config["repair_concurrency"] = concurrency
    receipt, _ = parallel.execute(
        config,
        context,
        queue,
        {"groups": []},
        initial,
        RunBudget({"max_inference_cost_usd": 100}),
    )
    assert receipt["created_prs"] == max(0, limit - 2)
    assert max(peaks) == min(concurrency or limit, limit)
    assert started == list(range(limit))
    assert receipt["stop_reason"] is None
    assert (
        sum(o["status"] == "deferred_pr_limit" for o in receipt["outcomes"])
        == 16 - limit
    )
    assert json.loads((tmp_path / "parallel-repairs.json").read_text())[
        "created_prs"
    ] == max(0, limit - 2)


def test_state_merge_preserves_sibling_results_and_publication_reservations(tmp_path):
    initial = state()
    first, sibling = topic(0), topic(1)
    prefix = "2026-W44/automation/"
    output = tmp_path / "topics/0"
    canonical = copy.deepcopy(initial)
    canonical["topic_weeks"][prefix + "1"] = {"status": "completed", "pr_url": "one"}
    canonical["events"]["event-1"] = {"topic_id": "1"}
    canonical["backlog"]["event-0"] = {"pending": True}
    path = tmp_path / "state.json"
    write_json(path, canonical)
    slot = {"status": "reserved", "report": str(output / "analysis/report.json")}
    parallel.reserve_shared(path, prefix + "0", slot)
    with pytest.raises(ValueError, match="owns"):
        parallel.reserve_shared(
            path, prefix + "0", slot | {"report": "/another/report"}
        )
    updated = copy.deepcopy(initial)
    updated["topic_weeks"][prefix + "0"] = {
        "status": "completed",
        "report": str(output / "topic-report.json"),
    }
    updated["events"]["event-0"] = {"topic_id": "0"}
    merged = parallel.merge_state(path, initial, updated, first, "2026-W44", output)
    assert merged["events"] == {
        "event-0": {"topic_id": "0"},
        "event-1": {"topic_id": "1"},
    }
    assert not merged["backlog"]
    assert (
        merged["topic_weeks"][prefix + sibling["id"]]
        == canonical["topic_weeks"][prefix + "1"]
    )
    assert merged["topic_weeks"][prefix + "0"]["status"] == "completed"


def test_merge_does_not_overwrite_a_different_publication_owner(tmp_path):
    initial, updated = state(), state()
    key = "2026-W44/automation/0"
    initial["topic_weeks"][key] = {"status": "retryable"}
    updated["topic_weeks"][key] = {"status": "completed"}
    path = tmp_path / "state.json"
    canonical = state()
    canonical["topic_weeks"][key] = {
        "status": "reserved",
        "report": str(tmp_path / "other/report"),
    }
    write_json(path, canonical)
    merged = parallel.merge_state(
        path, initial, updated, topic(0), "2026-W44", tmp_path / "topics/0"
    )
    assert merged["topic_weeks"] == canonical["topic_weeks"]


def test_child_worker_preserves_result_and_budget_on_failure(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        args[-1].record(0.12)
        raise ValueError("no repair")

    monkeypatch.setattr(weekly, "investigate", fail)
    request = tmp_path / "request.json"
    write_json(
        request,
        {
            "state": state(),
            "budget": {},
            "topic": topic(0),
            "config": {},
            "aggregation": {},
            "context": {
                "inventory": {},
                "records": [],
                "prs": [],
                "output": str(tmp_path),
                "week": "2026-W44",
                "dry_run": False,
                "limit": 3,
                "seed": 42,
            },
        },
    )
    parallel.worker(request)
    result = json.loads((tmp_path / "result.json").read_text())
    assert result["error"] == "no repair"
    assert result["budget"]["reported_inference_cost_usd"] == 0.12


@pytest.mark.parametrize("name", ["max_prs_per_run", "repair_concurrency"])
@pytest.mark.parametrize("value", [0, -1, True, 1.5, "10"])
def test_top_level_dispatch_parameters_fail_before_access(name, value):
    with pytest.raises(ValueError, match=name):
        weekly.run({name: value})
