import json
import sys
from pathlib import Path

import pytest

from test_automation import PATCH, arguments, command, fix_report, load_source
from test_automation import source as source_fixture
import patches


@pytest.fixture
def source(tmp_path):
    return source_fixture.__wrapped__(tmp_path)


@pytest.mark.parametrize(
    "repair_value,expected,configured",
    [(4, True, True), (5, False, True), (4, False, False)],
)
def test_real_comparison_gates_publication_and_attaches_recorded_evidence(
    source, tmp_path, monkeypatch, repair_value, expected, configured
):
    checkout, path, config = source
    runner = tmp_path / "compare.py"
    runner.write_text(
        "import ast,hashlib,json,pathlib,subprocess,sys\n"
        "p=pathlib.Path(sys.argv[2]);r=json.loads(p.read_text());o=pathlib.Path(sys.argv[4]);o.mkdir()\n"
        "def value(sha):\n t=subprocess.check_output(['git','show',sha+':value.py'],cwd=r['checkout'],text=True);return ast.literal_eval(t.split('=',1)[1].strip())\n"
        "before,after=value(r['baseline']),value(r['candidate'])\n"
        "status='improvement_on_development_cases' if before!=4 and after==4 and min(before,after)>=0 else 'inconclusive'\n"
        "v={k:r[k]for k in ('baseline','candidate')};v.update(status=status,markdown=f'Observed behavior: baseline {before}; candidate {after}. Control passed.',request_sha256=hashlib.sha256(p.read_bytes()).hexdigest())\n"
        "(o/'evidence.json').write_text(json.dumps(v));(p.parent/'executions.txt').write_text('one comparison')\n"
    )
    config.update(
        require_improvement=True,
        comparative_validation={"command": [sys.executable, str(runner)]},
    )
    if not configured:
        config.pop("require_improvement")
        config.pop("comparative_validation")
    path.write_text(json.dumps(config))
    command(
        ["git", "remote", "add", "origin", "https://github.com/Example/automation.git"],
        checkout,
    )
    args = arguments(tmp_path / "analysis", source_config=path, publish_draft=True)
    args.output.mkdir()
    pushed, created, bodies = [], [], []
    original = patches.command

    def github(argv, cwd=None, **kwargs):
        if argv[:2] == ["gh", "api"] and "/git/ref/heads/" in argv[2]:
            return config["revision"]
        if argv[:3] == ["gh", "pr", "list"]:
            return "[]"
        if argv[:2] == ["git", "ls-remote"]:
            return ""
        if argv[:2] == ["git", "push"]:
            pushed.append(argv)
            return ""
        if argv[:3] == ["gh", "pr", "create"]:
            created.append(argv)
            bodies.append(Path(argv[argv.index("--body-file") + 1]).read_text())
            assert "[Evidence]" in argv[argv.index("--title") + 1]
            assert "--draft" in argv
            assert argv[argv.index("--label") + 1] == "automation-improvement"
            return "https://github.com/Example/automation/pull/1"
        if argv[:3] == ["gh", "api", "repos/Example/automation/pulls/1"]:
            request = json.loads((args.output / "comparison-request.json").read_text())
            return json.dumps(
                {
                    "head": {"sha": request["candidate"]},
                    "body": bodies[-1],
                    "state": "open",
                }
            )
        if argv[:4] == ["gh", "api", "--method", "PATCH"]:
            bodies.append(json.loads(Path(argv[-1]).read_text())["body"])
            return ""
        return original(argv, cwd, **kwargs)

    monkeypatch.setattr(patches, "command", github)
    report = fix_report(PATCH.replace("+VALUE = 4", f"+VALUE = {repair_value}"))
    report["findings"][0]["trace_urls"] = ["https://example.test/trace"]
    result = patches.propose_fix(args, report, {"source": load_source(path)})
    assert bool(created) == bool(pushed) == expected
    if configured:
        assert (args.output / "executions.txt").read_text() == "one comparison"
    else:
        assert not (args.output / "executions.txt").exists()
    assert (checkout / "value.py").read_text() == "VALUE = 3\n"
    assert not (args.output / "candidate").exists()
    assert (args.output / "candidate.bundle").is_file()
    command(
        ["git", "bundle", "verify", str((args.output / "candidate.bundle").resolve())],
        checkout,
    )
    if expected:
        assert "Before / After Evidence" in bodies[-1]
        assert "baseline 3; candidate 4" in bodies[-1]
        assert result["comparison"]["published"]
    else:
        assert result["status"] == "improvement_not_demonstrated"
        assert result["behavioral_evaluation"] == "inconclusive"
