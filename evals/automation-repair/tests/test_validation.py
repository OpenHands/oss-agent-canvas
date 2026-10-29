import hashlib
import io
import json
import tarfile
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

from evaluation import assess
from repository_files import RepositoryFiles, materialize
from triage_world import World, scenario, score
from triage_helpers import deliver
from validation_agent import Executions


def test_labels_alone_and_rejected_ready_body_update_never_pass():
    world = World(scenario("reported_paid_harm"))
    target = f"/repos/{world.repository}/issues/7"
    world.request("GET", target)
    assert (
        world.request("PUT", target, {"body": "A body that was never saved"})[0] == 404
    )
    world.request(
        "POST", target + "/labels", {"labels": ["priority:medium", "ready-for-dev"]}
    )
    for suffix in ("", "/comments", "/labels"):
        world.request("GET", target + suffix)
    result = score(world.snapshot(), finished=True, final_response="READY")
    assert not result["passed"] and not result["task_achieved"]
    assert "missing_ready_deliverable" in result["violations"]
    assert result["error"] is None
    assert result["failed_write_attempts"][0]["method"] == "PUT"


@pytest.mark.parametrize("ready", [False, True])
def test_complete_triage_deliverables_and_readback_are_required(ready):
    case = scenario("reported_paid_harm") | {"readiness_expectation": "unspecified"}
    world = World(case)
    target = f"/repos/{world.repository}/issues/7"
    world.request("GET", target)
    world.request("POST", target + "/labels", {"labels": ["priority:medium"]})
    deliver(world, ready=ready)
    assert score(world.snapshot(), finished=True, final_response="Done")["passed"]
    world.request("POST", target + "/labels", {"labels": ["new-unrelated"]})
    assert (
        "final_state_not_verified"
        in score(world.snapshot(), finished=True, final_response="Done")["violations"]
    )


def test_repository_tree_files_and_workspace_are_the_captured_repository(tmp_path):
    archive = tmp_path / "repository.tar.gz"
    contents = {
        "AGENTS.md": "Read src/worker.py",
        "src/worker.py": "REAL_IMPLEMENTATION = True\n",
    }
    with tarfile.open(archive, "w:gz") as bundle:
        for name, value in contents.items():
            data = value.encode()
            info = tarfile.TarInfo("owner-project-sha/" + name)
            info.size = len(data)
            bundle.addfile(info, io.BytesIO(data))
    index = tmp_path / "repository-index.json"
    index.write_text(
        json.dumps({"tree": [{"path": name, "type": "blob"} for name in contents]})
    )
    snapshot = {
        "revision": "a" * 40,
        "files": {"AGENTS.md": contents["AGENTS.md"]},
        **{
            kind + "_path": str(path)
            for kind, path in (("archive", archive), ("index", index))
        },
        **{
            kind + "_sha256": hashlib.sha256(path.read_bytes()).hexdigest()
            for kind, path in (("archive", archive), ("index", index))
        },
    }
    files = RepositoryFiles(snapshot)
    assert files.contents("src")[1][0]["path"] == "src/worker.py"
    assert files.read("src/worker.py") == contents["src/worker.py"].encode()
    assert files.contents("arithmetic.py")[0] == 404
    case = {
        "repository_snapshot": snapshot,
        "native_payload": {"repository": "Example/project"},
    }
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "arithmetic.py").write_text("Fake code")
    materialize(case, workspace)
    assert (workspace / "src/worker.py").exists() and not (
        workspace / "arithmetic.py"
    ).exists()
    with pytest.raises(ValueError, match="Unsafe"):
        files.read("../secrets")
    archive.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="changed"):
        RepositoryFiles(snapshot)


def rows(case, violation=None):
    return [
        {
            "case_id": case,
            "variant": variant,
            "repeat": 1,
            "score": {
                "finished": True,
                "error": None,
                "violations": [violation]
                if violation and variant == "baseline"
                else [],
                "passed": not (violation and variant == "baseline"),
            },
        }
        for variant in ("baseline", "candidate")
    ]


def test_mechanism_evidence_and_regressions_survive_unrelated_invalid_trials():
    cases = [
        {"id": "moved", "kind": "regression", "target_violation": "wrong_head"},
        {"id": "control", "kind": "control"},
    ]
    executions = rows("moved", "wrong_head") + rows("control")
    executions[-1]["score"].update(finished=False, error="Provider timeout")
    result = assess({"runs": executions}, cases)
    assert result["mechanism_supported"] and not result["experiment_valid"]
    assert result["status"] == "inconclusive"
    executions[-1]["score"].update(
        finished=True, error=None, passed=False, violations=["new_failure"]
    )
    result = assess({"runs": executions}, cases)
    assert result["mechanism_supported"] and result["observed_regressions"]
    assert result["status"] == "regressed"
    assert (
        assess({"runs": executions[:2]}, cases[:1], unchanged=True)["claim"]
        != "mechanism_tested"
    )


def test_native_prompt_improvement_requires_two_valid_observations_and_no_placebo_win():
    cases = [
        {
            "id": "negative-1",
            "kind": "regression",
            "semantic_required": True,
            "repository_snapshot": {"archive_path": "/pinned"},
        },
        {
            "id": "non_negative-1",
            "kind": "control",
            "semantic_required": True,
            "repository_snapshot": {"archive_path": "/pinned"},
        },
    ]
    executions, judgments = [], []
    for repeat in (1, 2):
        for case in cases:
            executions += [row | {"repeat": repeat} for row in rows(case["id"])]
            judgments.append(
                {
                    "case_id": case["id"],
                    "repeat": repeat,
                    "context_sufficient": True,
                    "baseline_success": case["kind"] != "regression",
                    "candidate_success": True,
                    "target_reproduced": case["kind"] == "regression",
                    "target_fixed": case["kind"] == "regression",
                    "regression": False,
                }
            )
    result = assess({"runs": executions}, cases, 2, judgments)
    assert result["status"] == "improvement_on_development_cases"
    assert not result["statistical_improvement_proven"]
    assert (
        assess({"runs": executions}, cases, 2, judgments, unchanged=True)["status"]
        == "inconclusive"
    )
    judgments[0]["target_fixed"] = None
    assert assess({"runs": executions}, cases, 2, judgments)["status"] == "inconclusive"


def test_execution_reader_cannot_read_arbitrary_paths(tmp_path):
    reader = Executions(tmp_path, [{"id": "negative-1"}])
    with pytest.raises(ValueError):
        reader.execute(
            "read_execution",
            {
                "case_id": "../outside",
                "variant": "baseline",
                "artifact": "world",
                "offset": 0,
            },
        )


def test_cross_repository_rest_and_graphql_reads_cannot_use_another_repository():
    case = scenario("reported_paid_harm") | {
        "native_payload": {"repository": "Owner/A", "task": {"issue": {"number": 7}}},
        "repository_snapshot": {"files": {"AGENTS.md": "Rules for A"}},
    }
    world = World(case)
    assert world.request("GET", "/repos/Other/B/contents/AGENTS.md")[0] == 404
    assert world.request("GET", "/repos/Other/B/git/trees/main")[0] == 404
    assert (
        world.request(
            "POST",
            "/graphql",
            {"query": '{repository(owner:"Other", name:"A"){issue(number:7){body}}}'},
        )[0]
        == 404
    )
    assert (
        world.request(
            "POST",
            "/graphql",
            {
                "query": "query($o:String!,$n:String!){repository(owner:$o,name:$n){issue(number:7){body}}}",
                "variables": {"o": "Owner", "n": "A"},
            },
        )[0]
        == 200
    )
    assert len(world.coverage_errors) == 3


def test_comment_pagination_does_not_repeat_page_one():
    world = World(scenario("reported_paid_harm"))
    world.comments = [
        {"id": i, "body": "Comment", "user": {"login": "human"}} for i in range(150)
    ]
    path = f"/repos/{world.repository}/issues/7/comments"
    _, first = world.request("GET", path + "?per_page=100&page=1")
    assert len(first) == 100 and "page=2" in world.response_headers["Link"]
    _, second = world.request("GET", path + "?per_page=100&page=2")
    assert len(second) == 50 and second[0]["id"] == 100 and not world.response_headers


def test_semantic_success_cannot_contradict_a_no_regression_checkbox():
    cases = [
        {
            "id": "negative-1",
            "kind": "regression",
            "semantic_required": True,
            "repository_snapshot": {"archive_path": "/pinned"},
        },
        {
            "id": "control",
            "kind": "control",
            "semantic_required": True,
            "repository_snapshot": {"archive_path": "/pinned"},
        },
    ]
    executions, judgments = [], []
    for repeat in (1, 2):
        for case in cases:
            executions += [row | {"repeat": repeat} for row in rows(case["id"])]
            judgments.append(
                {
                    "case_id": case["id"],
                    "repeat": repeat,
                    "context_sufficient": True,
                    "baseline_success": case["kind"] == "control",
                    "candidate_success": case["kind"] != "control",
                    "target_reproduced": case["kind"] == "regression",
                    "target_fixed": case["kind"] == "regression",
                    "regression": False,
                    "executed_change": True,
                }
            )
    assert assess({"runs": executions}, cases, 2, judgments)["status"] == "regressed"
    for review in judgments:
        review.update(
            baseline_success=False, candidate_success=True, executed_change=False
        )
    assert assess({"runs": executions}, cases, 2, judgments)["status"] == "inconclusive"


def test_missing_file_and_directory_reads_are_recoverable(tmp_path):
    reader = Executions(
        tmp_path,
        [
            {
                "id": "negative-1",
                "native_payload": {"repository": "Example/project"},
                "repository_snapshot": {"files": {"src/worker.py": "Real code"}},
            }
        ],
    )
    assert (
        reader.execute(
            "read_repository",
            {"repository": "Example/project", "path": "missing.py", "offset": 0},
        )["status"]
        == 404
    )
    directory = reader.execute(
        "read_repository", {"repository": "Example/project", "path": "src", "offset": 0}
    )
    assert (
        directory["status"] == 200
        and directory["contents"][0]["path"] == "src/worker.py"
    )


@pytest.mark.parametrize("bad_control", [False, True])
def test_semantic_agent_is_in_the_real_receipt_path(tmp_path, monkeypatch, bad_control):
    import sys
    from types import SimpleNamespace
    import pr_comparison
    from compare import compare, write
    from validation_agent import worker_path

    sys.path.insert(0, str(worker_path({})))
    import evidence_agent

    archive = tmp_path / "repository.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        value = b"Resolve scope from code"
        info = tarfile.TarInfo("repo/AGENTS.md")
        info.size = len(value)
        bundle.addfile(info, io.BytesIO(value))
    index = tmp_path / "index.json"
    index.write_text(json.dumps({"tree": [{"path": "AGENTS.md", "type": "blob"}]}))
    snapshot = {
        "files": {"AGENTS.md": "Resolve scope from code"},
        "archive_path": str(archive),
        "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "index_path": str(index),
        "index_sha256": hashlib.sha256(index.read_bytes()).hexdigest(),
    }
    cases = [
        {
            "id": name,
            "family": "triage",
            "kind": kind,
            "native_payload": {
                "repository": "Example/project",
                "task": {"issue": {"author_body": "A real workflow failure"}},
            },
            "repository_snapshot": snapshot,
            "historical_sample_size": 1,
            "trace_url": "https://example.test/trace/" + name,
            "semantic_required": True,
        }
        for name, kind in (("negative-1", "regression"), ("non_negative-1", "control"))
    ]
    request = {
        "repository": "Example/project",
        "baseline": "a" * 40,
        "candidate": "b" * 40,
        "changed_files": ["plugins/issue-triage/scripts/worker.py"],
        "topic": {"key": "actual-workflow-priority"},
        "plan": {"base_url": "https://proxy.example.test", "repeats": 2},
    }
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request))
    monkeypatch.setattr(
        pr_comparison,
        "settings",
        lambda req, output: (
            SimpleNamespace(native_cases=cases, repeats=2, output=output),
            True,
        ),
    )

    def execute(args):
        executions = []
        for repeat in (1, 2):
            for case in cases:
                for row in rows(case["id"]):
                    row.update(
                        repeat=repeat,
                        final_response="Done",
                        measurements={"seconds": 0},
                    )
                    executions.append(row)
                    directory = args.output / f"{case['id']}-{repeat}-{row['variant']}"
                    write(
                        directory / "world.json",
                        {"issue": {"body": "Complete task"}, "comments": []},
                    )
                    write(
                        directory / "prompt.json",
                        {"prompt": row["variant"] + " policy"},
                    )
        write(
            args.output / "manifest.json",
            {
                "baseline": request["baseline"],
                "candidate": request["candidate"],
                "model": "test-model",
                "effort": "high",
                "repeats": 2,
                "cases": cases,
            },
        )
        write(args.output / "comparison.json", compare(executions, cases, 2))

    def review(args, model, instruction, context, name, schema, reader):
        args.output.mkdir(parents=True)
        (args.output / "validation.json").write_text("retained audit")
        missing = reader.execute(
            "read_repository",
            {"repository": "Example/project", "path": "absent.py", "offset": 0},
        )
        assert missing["status"] == 404
        results = []
        for case in cases:
            for repeat in (1, 2):
                results.append(
                    {
                        "case_id": case["id"],
                        "repeat": repeat,
                        "context_sufficient": True,
                        "baseline_success": case["kind"] == "control",
                        "candidate_success": not (
                            bad_control and case["kind"] == "control"
                        ),
                        "target_reproduced": case["kind"] == "regression",
                        "target_fixed": case["kind"] == "regression",
                        "regression": False,
                        "reason": "Paired task evidence",
                        "evidence": [
                            f"{case['id']}:{variant}:{repeat}:world"
                            for variant in ("baseline", "candidate")
                        ],
                    }
                )
        return {"cases": results}, {"reported_cost_usd": "0"}

    monkeypatch.setattr(pr_comparison.compare, "main", execute)
    monkeypatch.setattr(evidence_agent, "investigate", review)
    result = pr_comparison.run(path, tmp_path / "comparison")
    assert result["status"] == (
        "regressed" if bad_control else "improvement_on_development_cases"
    )
    assert (tmp_path / "comparison/semantic-assessment.json").exists()
    assert result["assessment"]["semantic_review_complete"]
    from validation_agent import assess

    source = tmp_path / "comparison/runs"
    report = json.loads((source / "comparison.json").read_text())
    recovery = tmp_path / "recovery"
    assess(request, source, report, cases, output=recovery)
    assert (recovery / "semantic-assessment.json").exists()
    assert (tmp_path / "comparison/semantic-review/validation.json").read_text() == (
        "retained audit"
    )


def test_graphql_cannot_substitute_another_issue_or_file_and_search_is_not_fake_empty():
    case = scenario("reported_paid_harm") | {
        "native_payload": {"repository": "Owner/A", "task": {"issue": {"number": 7}}},
        "repository_snapshot": {"files": {"AGENTS.md": "Rules for A"}},
    }
    world = World(case)
    assert (
        world.request(
            "POST",
            "/graphql",
            {"query": '{repository(owner:"Owner",name:"A"){issue(number:1234){body}}}'},
        )[0]
        == 404
    )
    assert (
        world.request(
            "POST",
            "/graphql",
            {
                "query": '{repository(owner:"Owner",name:"A"){object(expression:"other:README.md"){text}}}'
            },
        )[0]
        == 404
    )
    assert world.request("GET", "/search/issues?q=missing")[0] == 404
    assert len(world.coverage_errors) == 3


def test_readback_tracks_each_mutated_resource_and_issue_reads_include_labels():
    from triage_contract import DISCLOSURE

    world = World(scenario("reported_paid_harm"))
    target = f"/repos/{world.repository}/issues/7"
    world.request("GET", target)
    world.request(
        "POST",
        target + "/comments",
        {
            "body": f"---\n{DISCLOSURE}\n### Decision needed\nWhich approval policy?\n<!-- triage-source:frozen-case -->"
        },
    )
    world.request("GET", target + "/comments")
    world.request("POST", target + "/labels", {"labels": ["priority:medium"]})
    world.request("GET", target)
    assert score(world.snapshot(), finished=True, final_response="Done")["passed"]
