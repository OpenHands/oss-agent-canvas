import copy
import json
import subprocess

import pytest

from backlog import issue_guides, namespace
from common import write_json
from finding_record import SCHEMA, read_batch
from triage import prepare, validate
from test_triage import topic
import triage_agent
import triage_workflow
import workflow_runtime


def batch(*rows):
    return {"schema": SCHEMA, "source": "ci-tests", "findings": list(rows)}


def row(key, **fields):
    return {
        "id": key,
        "observed_at": "2026-11-24T12:00:00Z",
        "summary": "Empty report",
        **fields,
    }


def test_portable_source_supersession_replay_and_private_reader(
    tmp_path, monkeypatch, capsys
):
    config = {
        "finding_source": "portable",
        "source_namespace": "ci-tests",
        "repository": "example/repo",
        "llm_base_url": "https://example.invalid",
    }
    monkeypatch.delenv("LMNR_PROJECT_API_KEY", raising=False)
    monkeypatch.delenv("LMNR_BASE_URL", raising=False)
    workflow_runtime.load_environment(config)
    state = {"events": {}}
    first = row("launch-1")
    second = row(
        "launch-2",
        group="publication",
        origins=["reporter", "publisher"],
        evidence="private captured fixture",
        supersedes=["launch-1"],
    )
    assert len(read_batch(config, batch(first), state)) == 1
    selected = read_batch(config, batch(second), state)
    assert selected == read_batch(config, batch(first, second), state)
    cards, refs, prepared = prepare(selected, [], [])
    assert cards["groups"][0]["events"] == 1
    assert cards["event_catalog"][0]["possible_origins"] == second["origins"]
    result = validate(
        {"topics": [topic("e1")], "no_action_reason": ""}, selected, refs, 1
    )[0]
    assert result["representative_event_ids"] == [selected[0]["occurrence_id"]]
    assert "private captured fixture" not in json.dumps(cards)
    records_path = tmp_path / "records.json"
    write_json(records_path, prepared)
    context = tmp_path / "context.json"
    write_json(
        context,
        {
            "finding_source": "portable",
            "issue": {"number": 0, "url": None, "body": "preview"},
            "repository": "example/repo",
            "issue_evidence_dir": str(tmp_path),
            "signal_records_path": str(records_path),
        },
    )
    triage_agent.evidence(context, "read_finding", {"event_ref": "e1"})
    assert "private captured fixture" in capsys.readouterr().out
    with pytest.raises(ValueError, match="Unknown prepared finding"):
        triage_agent.evidence(context, "read_finding", {"event_ref": "e2"})
    state["events"][selected[0]["occurrence_id"]] = {"topic_id": result["id"]}
    assert not read_batch(config, batch(first, second), state)
    assert namespace(config, tmp_path) != namespace(
        config | {"source_namespace": "another"}, tmp_path
    )


def test_portable_canonical_issue_and_distinct_units():
    config = {"source_namespace": "ci-tests"}
    url = "https://github.com/Example/source/issues/1"
    rows = read_batch(
        config,
        batch(row("a", issue_url=url.lower(), unit="job"), row("b", unit="job")),
        {},
    )
    _, refs, _ = prepare(rows, [{"url": url, "kind": "issue"}], [])
    plan = topic(
        next(
            ref
            for ref, key in refs["events"].items()
            if key == rows[0]["occurrence_id"]
        )
    )
    output = {"topics": [plan], "no_action_reason": ""}
    assert validate(output, rows, refs, 1)[0]["existing_work_urls"] == [url]
    plan["representative_event_refs"] = ["e1", "e2"]
    with pytest.raises(ValueError, match="repeat"):
        validate(output, rows, refs, 2)


@pytest.mark.parametrize("handled", [False, True])
def test_occurrences_are_immutable_and_batch_rejection_is_atomic(handled):
    config, state = {"source_namespace": "ci-tests"}, {"events": {}}
    original = row("a", evidence="captured", metadata={"z": 1, "a": 2})
    saved = read_batch(config, batch(original), state)
    if handled:
        state["events"][saved[0]["occurrence_id"]] = {"topic_id": "complete"}
    replay = row("a", evidence="captured", metadata={"a": 2, "z": 1})
    assert read_batch(config, batch(replay), state) == ([] if handled else saved)
    before = copy.deepcopy(state)
    with pytest.raises(ValueError, match="changed"):
        read_batch(config, batch(row("b"), original | {"supersedes": ["b"]}), state)
    assert state == before


def test_supersession_cannot_form_a_cycle_or_fork_across_batches():
    config, state = {"source_namespace": "ci-tests"}, {}
    read_batch(
        config, batch(row("b", supersedes=["a"]), row("c", supersedes=["b"])), state
    )
    before = copy.deepcopy(state)
    for value, error in (
        (row("a", supersedes=["c"]), "cycle"),
        (row("d", supersedes=["a"]), "different successor"),
    ):
        with pytest.raises(ValueError, match=error):
            read_batch(config, batch(value), state)
        assert state == before


def test_guide_reader_uses_pinned_bare_repository_and_only_catalogued_paths(
    tmp_path, capsys
):
    bare = tmp_path / "bare.git"
    subprocess.run(
        ["git", "init", "--bare", str(bare)], check=True, capture_output=True
    )

    def git(*args, data=None):
        return subprocess.run(
            [
                "git",
                "-C",
                str(bare),
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                *args,
            ],
            input=data,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    def commit(guide):
        blob = git("hash-object", "-w", "--stdin", data=guide)
        tree = git(
            "mktree",
            data=f"100644 blob {blob}\tCONTRIBUTING.md\n100644 blob {blob}\tother.txt\n",
        )
        return git("commit-tree", tree, "-m", "Guide fixture")

    pinned, current = commit("pinned guide\n"), commit("changed guide\n")
    git("update-ref", "refs/heads/main", current)
    git("remote", "add", "origin", "https://github.com/Example/source.git")
    config = {
        "repository": "Example/source",
        "checkout": str(bare),
        "_source_revision": pinned,
    }
    catalog = triage_workflow.repository_catalog(config)
    source = catalog[config["repository"]]
    assert source["revision"] == pinned
    guides = issue_guides(source)
    assert guides == ["CONTRIBUTING.md"]
    context = tmp_path / "context.json"
    write_json(
        context,
        {
            "repository": config["repository"],
            "repositories": catalog,
            "issue_guide_files": guides,
            "issue_evidence_dir": str(tmp_path),
            "issue": {"number": 0, "url": None, "body": ""},
        },
    )
    triage_agent.evidence(
        context, "read_repository_guide", {"path": guides[0], "offset": 0}
    )
    assert json.loads(capsys.readouterr().out)["content"] == "pinned guide\n"
    with pytest.raises(ValueError, match="prepared issue guide"):
        triage_agent.evidence(context, "read_repository_guide", {"path": "other.txt"})
