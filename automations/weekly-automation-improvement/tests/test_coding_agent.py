import importlib
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from test_automation import arguments, command, fix_report, load_source, PATCH
from test_automation import source as source_fixture
from budget import RunBudget
import coding_agent
import patches

source = source_fixture


def install_sdk(monkeypatch, execute):
    monkeypatch.setattr(coding_agent, "version", lambda name: "1.56.0")
    captured = {}
    agent = SimpleNamespace(
        tools=[
            SimpleNamespace(name=name)
            for name in ("terminal", "file_editor", "browser", "task")
        ]
    )

    class Conversation:
        def __init__(self, **kwargs):
            assert "max_budget_per_run" not in kwargs
            captured["conversation"] = kwargs
            captured["instance"] = self
            self.state = SimpleNamespace(
                execution_status=SimpleNamespace(value="finished")
            )
            self.id = "native-sdk-conversation"
            self.conversation_stats = SimpleNamespace(
                get_combined_metrics=lambda: SimpleNamespace(accumulated_cost=0.12)
            )

        def send_message(self, prompt):
            captured["prompt"] = prompt

        def run(self):
            execute(Path(captured["conversation"]["workspace"]))

        def close(self):
            captured["closed"] = True

    modules = {
        "openhands": {},
        "openhands.sdk": {
            "Conversation": Conversation,
            "LLM": lambda **kwargs: captured.update(llm=kwargs) or object(),
        },
        "openhands.sdk.tool": {"Tool": lambda name: SimpleNamespace(name=name)},
        "openhands.tools": {},
        "openhands.tools.preset": {},
        "openhands.tools.preset.default": {
            "get_default_agent": lambda **kwargs: agent,
            "register_builtins_agents": lambda: None,
        },
        "openhands.tools.task": {"TaskToolSet": SimpleNamespace(name="task")},
    }
    for name, values in modules.items():
        module = ModuleType(name)
        module.__dict__.update(values)
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setenv("LLM_BASE_URL", "https://example.test")
    monkeypatch.setenv("LITELLM_API_KEY", "private-test-key")
    return captured


def test_native_agent_captures_actual_large_multifile_diff_and_budget(
    source, tmp_path, monkeypatch
):
    checkout, path, config = source
    config["runtime"] = {"automation": {"timeout": 1200, "trigger": {"type": "event"}}}
    path.write_text(json.dumps(config))
    output = tmp_path / "repair"
    output.mkdir()

    def execute(worktree):
        (worktree / "value.py").write_text("VALUE = 4\n")
        for index in range(6):
            (worktree / f"module_{index}.py").write_text(
                "DATA = " + repr("x" * 5000) + "\n"
            )
        (output / "coding-agent/result.json").write_text(
            json.dumps({"proposals": [{"title": "Real edits"}], "rejections": []})
        )

    captured = install_sdk(monkeypatch, execute)
    args = arguments(output, source_config=path, coordinator="openai/gpt-6.1-sol")
    args.budget = RunBudget({"max_inference_cost_usd": 8, "max_run_seconds": 100})
    result, measurement, patch = coding_agent.coordinate(
        args, {"findings": []}, {"source": load_source(path)}
    )
    assert result["proposals"]
    assert patch.count("diff --git") == 7 and len(patch) > 20000
    assert captured["llm"]["model"] == "openai/gpt-6.1-sol"
    assert captured["instance"].max_budget_per_run == 8
    assert measurement["tools"] == ["terminal", "file_editor", "browser", "task"]
    brief = json.loads((output / "coding-agent/context.json").read_text())
    assert brief["snapshot"]["source"]["runtime"] == config["runtime"]
    assert args.budget.cost == 0.12 and captured["closed"]
    assert (checkout / "value.py").read_text() == "VALUE = 3\n"
    assert not (output / "coding-worktree").exists()
    assert (output / "coding-agent/events.json").stat().st_mode & 0o077 == 0


def test_sdk_initialization_failure_cleans_worktree(source, tmp_path, monkeypatch):
    checkout, path, _ = source
    captured = install_sdk(monkeypatch, lambda _: None)
    module = importlib.import_module("openhands.tools.preset.default")

    def fail(**kwargs):
        raise RuntimeError("initialization failed")

    monkeypatch.setattr(module, "get_default_agent", fail)
    args = arguments(tmp_path / "repair", source_config=path)
    args.output.mkdir()
    with pytest.raises(RuntimeError, match="initialization failed"):
        coding_agent.coordinate(args, {}, {"source": load_source(path)})
    assert not (args.output / "coding-worktree").exists()
    assert (
        command(["git", "worktree", "list", "--porcelain"], checkout).count("worktree ")
        == 1
    )
    assert "closed" not in captured


def test_identical_new_tests_execute_production_code_on_both_revisions(
    source, tmp_path
):
    _, path, config = source
    config["editable_roots"] = ["tests/"]
    path.write_text(json.dumps(config))
    extra = 'diff --git a/tests/test_value.py b/tests/test_value.py\nnew file mode 100644\n--- /dev/null\n+++ b/tests/test_value.py\n@@ -0,0 +1,3 @@\n+import runpy\n+value = runpy.run_path("value.py")["VALUE"]\n+assert value == 4\n'
    report = fix_report(PATCH + extra)
    report["proposals"][0]["checks"] = [
        {
            "name": "target",
            "argv": [sys.executable, "tests/test_value.py"],
            "baseline": "fail",
        },
        config["checks"][1],
    ]
    args = arguments(tmp_path / "repair", source_config=path)
    args.output.mkdir()
    result = patches.propose_fix(args, report, {"source": load_source(path)})
    assert [row["exit_code"] for row in result["baseline"]] == [1, 0]
    assert [row["exit_code"] for row in result["candidate"]] == [0, 0]
    assert "AssertionError" in result["baseline"][0]["excerpt"]
    assert result["test_assets_sha256"]["tests/test_value.py"]


def test_dangling_symlink_is_rejected_before_checks(source, tmp_path, monkeypatch):
    _, path, config = source
    config["editable_roots"] = ["tests/"]
    path.write_text(json.dumps(config))
    patch = "diff --git a/tests/link b/tests/link\nnew file mode 120000\n--- /dev/null\n+++ b/tests/link\n@@ -0,0 +1 @@\n+missing\n"
    monkeypatch.setattr(
        patches, "run_checks", lambda *a: pytest.fail("must not execute")
    )
    args = arguments(tmp_path / "repair", source_config=path)
    args.output.mkdir()
    with pytest.raises(ValueError, match="regular source files"):
        patches.propose_fix(args, fix_report(patch), {"source": load_source(path)})


def test_known_credentials_block_public_patch(monkeypatch):
    monkeypatch.setenv("LMNR_PROJECT_API_KEY", "private-project-credential-value")
    with pytest.raises(ValueError, match="credential"):
        patches.validate_public_patch("+TOKEN = 'private-project-credential-value'")
    patches.validate_public_patch("+TOKEN = os.environ['LMNR_PROJECT_API_KEY']")


def test_evidence_review_is_independent_and_can_reject_import_failure(
    tmp_path, monkeypatch
):
    args = arguments(tmp_path)

    def model(args, model, policy, context, name, schema):
        assert "missing imports/files/dependencies" in policy
        assert context["baseline"][0]["excerpt"] == "ModuleNotFoundError"
        assert schema["properties"]["public_safe"] == {"type": "boolean"}
        return {
            "supported": False,
            "public_safe": True,
            "reason": "No production behavior executed",
        }, {}

    monkeypatch.setattr(patches, "call_model", model)
    review = patches.review_checks(
        args, {"patch": PATCH}, [], [], [{"excerpt": "ModuleNotFoundError"}], [], {}
    )
    assert not review["supported"]


@pytest.mark.parametrize(
    "supported,public_safe", [(False, True), (True, False), (True, True)]
)
def test_publication_requires_review_and_preserves_historical_comparison(
    source, tmp_path, monkeypatch, supported, public_safe
):
    checkout, path, config = source
    config["comparative_validation"] = {"command": ["unused"]}
    path.write_text(json.dumps(config))
    command(
        ["git", "remote", "add", "origin", "https://github.com/Example/automation.git"],
        checkout,
    )
    args = arguments(tmp_path / "repair", source_config=path, publish_draft=True)
    args.output.mkdir()
    report = fix_report(PATCH)
    report["findings"][0].update(trace_urls=[], case_id="c1")
    report["proposals"][0]["checks"] = config["checks"]
    original = patches.command
    calls = []

    def transport(argv, cwd=None, **kwargs):
        if argv[:2] == ["gh", "api"]:
            return config["revision"]
        if argv[:3] == ["gh", "pr", "list"]:
            return "[]"
        if argv[:2] == ["git", "ls-remote"]:
            return ""
        if argv[:2] == ["git", "push"]:
            calls.append("push")
            return ""
        if argv[:3] == ["gh", "pr", "create"]:
            calls.append("create")
            assert "[Mechanism tested]" in argv[argv.index("--title") + 1]
            assert "automation-improvement" in argv
            body = Path(argv[argv.index("--body-file") + 1]).read_text()
            assert (
                "Historical input comparison" in body
                and "Historical inconclusive" in body
            )
            return "https://github.com/Example/automation/pull/2"
        return original(argv, cwd, **kwargs)

    def historical(*args, **kwargs):
        calls.append("historical")
        return {"status": "inconclusive", "markdown": "Historical inconclusive"}

    monkeypatch.setattr(patches, "command", transport)
    monkeypatch.setattr(
        patches,
        "review_checks",
        lambda *a: {
            "supported": supported,
            "public_safe": public_safe,
            "reason": "Independent review",
        },
    )
    monkeypatch.setattr(patches, "before_publication", historical)
    result = patches.propose_fix(args, report, {"source": load_source(path)})
    if supported and public_safe:
        assert calls == ["historical", "push", "create"]
        assert result["status"] == "evidence_supported_draft"
    else:
        assert calls == [] and result["status"] == "rejected"
        assert (args.output / "paired-review-failure.json").exists()


def test_failed_native_attempt_consumes_shared_budget(source, tmp_path, monkeypatch):
    _, path, _ = source

    def execute(worktree):
        (worktree / "value.py").write_text("VALUE = 4\n")
        raise RuntimeError("native model failed")

    captured = install_sdk(monkeypatch, execute)
    args = arguments(tmp_path / "repair", source_config=path)
    args.output.mkdir()
    args.budget = RunBudget({"max_inference_cost_usd": 8, "max_run_seconds": 100})
    with pytest.raises(RuntimeError, match="native model failed"):
        coding_agent.coordinate(args, {}, {"source": load_source(path)})
    assert args.budget.cost == 0.12 and captured["closed"]
    assert not (args.output / "coding-worktree").exists()
    measurement = json.loads(
        (args.output / "coding-agent/measurement.json").read_text()
    )
    assert measurement["sdk_estimated_cost_usd"] == 0.12
    worktree = json.loads((args.output / "coding-agent/worktree.json").read_text())
    assert "+VALUE = 4" in worktree["patch"]


def test_issue_reads_do_not_require_laminar_credentials(tmp_path, monkeypatch, capsys):
    import topic_issue
    from test_topic_issue import GitHub, snapshot

    monkeypatch.delenv("LMNR_PROJECT_API_KEY", raising=False)
    monkeypatch.delenv("LMNR_BASE_URL", raising=False)
    monkeypatch.setattr(topic_issue, "command", GitHub())
    value = snapshot(tmp_path)
    issue = topic_issue.TopicIssue.ensure(value, tmp_path / "issue")
    value["repair_issue"] = issue.refresh()
    context = tmp_path / "context.json"
    context.write_text(json.dumps({"snapshot": value, "inventory": {}}))
    coding_agent.evidence(context, "read_issue", {"section": "Goal", "offset": 0})
    result = json.loads(capsys.readouterr().out)
    assert result["content"].startswith("## Goal")


def test_brief_removes_duplicate_traces_without_losing_originals(
    source, tmp_path, monkeypatch
):
    _, path, _ = source
    output = tmp_path / "repair"
    output.mkdir()
    original = {"source": load_source(path), "samples": [{"raw": "trace-" * 10000}]}
    captured = install_sdk(
        monkeypatch,
        lambda wt: (output / "coding-agent/result.json").write_text(
            '{"proposals":[],"rejections":[]}'
        ),
    )
    args = arguments(output, source_config=path)
    coding_agent.coordinate(
        args,
        {"source": original["source"], "cases": [{"original_task": "task"}]},
        original,
    )
    brief_path = output / "coding-agent/context.json"
    brief = json.loads(brief_path.read_text())
    assert brief_path.stat().st_size < 4000
    assert brief["context"]["cases"] == [{"original_task": "task"}]
    assert brief["snapshot"]["source"]["files"] == ["value.py"]
    assert json.loads(Path(brief["snapshot_path"]).read_text()) == original
    assert json.loads(Path(brief["inventory_path"]).read_text()) == {}
    assert "targeted JSON extraction" in captured["prompt"]


def test_independent_review_receives_operational_constraints(tmp_path, monkeypatch):
    runtime = {"automation": {"timeout": 1200, "trigger": {"type": "event"}}}
    captured = []

    def review(args, model, prompt, context, *rest):
        captured.append((prompt, context))
        return {
            "supported": False,
            "public_safe": True,
            "reason": "Missing timeout margin",
        }, {}

    monkeypatch.setattr(patches, "call_model", review)
    result = patches.review_checks(
        arguments(tmp_path, coordinator="sol"),
        {"patch": ""},
        [],
        [],
        [],
        [],
        {"source": {"runtime": runtime}},
    )
    assert not result["supported"]
    assert captured[0][1]["runtime"] == runtime
    assert "dispatch/concurrency" in captured[0][0]
