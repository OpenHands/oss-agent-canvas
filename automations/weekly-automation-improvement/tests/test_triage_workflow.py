from pathlib import Path

import triage_workflow
from common import read_json


def test_triage_publishes_topics_without_repair_code(prepared, monkeypatch):
    import sys

    config, topics = prepared
    monkeypatch.setitem(sys.modules, "repair_workflow", None)
    monkeypatch.setitem(sys.modules, "parallel", None)
    published = []

    def publish(config, context, topic, existing, output):
        published.append(topic)
        assert existing is None
        assert Path(context["inventory_path"]).is_file()
        return f"https://github.com/Example/source/issues/{len(published)}"

    monkeypatch.setattr(triage_workflow, "publish", publish)
    monkeypatch.setattr(triage_workflow, "existing_work", lambda _: [])
    result = triage_workflow.run(config, seed=42)
    assert result["status"] == "published"
    assert published == topics
    assert result["budget"]["reported_inference_cost_usd"] == 0.1
    state = read_json(Path(config["state_dir"]) / "state.json")
    assert not state["topic_weeks"]
    assert not state["backlog"]
    assert len(state["events"]) == 3
    output = Path(result["output"])
    assert not (output / "repair-request.json").exists()
    assert not (output / "workers").exists()
    assert triage_workflow.run(config)["status"] == "no_unhandled_findings"
    assert len(published) == 3


def test_triage_preview_does_not_publish_topics(prepared, monkeypatch):
    import pytest

    config, _ = prepared
    monkeypatch.setattr(
        triage_workflow, "publish", lambda *a, **k: pytest.fail("publish")
    )
    result = triage_workflow.run(config, dry_run=True)
    assert result["status"] == "preview"
    assert not (Path(config["state_dir"]) / "state.json").exists()
