import sys
import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "tarball"))


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    import triage_workflow
    import workflow_runtime
    from common import write_json
    from test_support import SIGNAL
    from test_weekly import inventory, record

    rows = [record(i) for i in range(1, 4)]
    frame = tmp_path / "inventory.json"
    write_json(frame, inventory(rows))
    config = {
        "repository": "Example/source",
        "checkout": str(tmp_path),
        "project_id": "00000000-0000-0000-0000-000000000001",
        "signal_id": SIGNAL,
        "input": str(frame),
        "state_dir": str(tmp_path / "state"),
        "max_topics_per_run": 2,
        "repair_concurrency": 2,
        "max_inference_cost_usd": 1,
    }
    monkeypatch.setattr(workflow_runtime, "load_environment", lambda _: None)
    monkeypatch.setattr(triage_workflow, "github_auth", lambda _: None)
    monkeypatch.setattr(triage_workflow, "repository_catalog", lambda _: {})
    monkeypatch.setattr(
        triage_workflow, "refresh_epic", lambda *args: {"status": "refreshed"}
    )
    queue = [
        {"id": str(i), "priority": i, "representative_event_ids": [r["event"]["id"]]}
        for i, r in enumerate(rows)
    ]

    def triage(args, records, prs, previous):
        args.budget.record(0.1)
        return queue, {
            "triage_issue": {"url": "https://github.com/Example/source/issues/1"}
        }

    monkeypatch.setattr(triage_workflow, "triage", triage)
    return config, queue
