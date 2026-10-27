import ast
import difflib
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

from common import read_json, redact, write_json


def patch_from_edits(source, edits):
    if not isinstance(edits, list) or len(edits) > 4:
        raise ValueError("Expected at most four exact source edits")
    if not edits:
        return ""
    if not source:
        raise ValueError("Source edits require frozen source")
    pieces, changed = [], set()
    for edit in edits:
        if not isinstance(edit, dict) or set(edit) != {"path", "before", "after"}:
            raise ValueError("Invalid source edit fields")
        path, before, after = (edit[key] for key in ("path", "before", "after"))
        if (
            not all(isinstance(value, str) for value in (path, before, after))
            or path not in source["files"]
            or not re.fullmatch(r"[\w./-]+", path)
            or Path(path).is_absolute()
            or ".." in Path(path).parts
            or path.startswith("-")
            or path in changed
            or not before
            or before == after
            or max(len(before), len(after)) > 20000
        ):
            raise ValueError("Invalid, duplicate or out-of-scope source edit")
        original = source["files"][path]
        position = original.find(before)
        if position < 0 or position != original.rfind(before):
            raise ValueError("Source edit must match exactly one location")
        updated = original.replace(before, after, 1)
        pieces.append(f"diff --git a/{path} b/{path}\n")
        for line in difflib.unified_diff(
            original.splitlines(keepends=True),
            updated.splitlines(keepends=True),
            fromfile="a/" + path,
            tofile="b/" + path,
        ):
            pieces.append(
                line
                if line.endswith("\n")
                else line + "\n\\ No newline at end of file\n"
            )
        changed.add(path)
    patch = "".join(pieces)
    if len(patch.encode()) > 20000:
        raise ValueError("Generated patch exceeds its byte budget")
    return patch


def command(argv, cwd=None, strip=True, **kwargs):
    output = subprocess.run(
        argv, cwd=cwd, capture_output=True, text=True, check=True, timeout=60, **kwargs
    ).stdout
    return output.strip() if strip else output


def load_source(config_path):
    config = read_json(config_path)
    repo = config["repository"]
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
        raise ValueError("Invalid configured GitHub repository")
    revision = config["revision"]
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("Source revision must be a full commit SHA")
    checkout = Path(config["checkout"]).expanduser().resolve()
    if (
        command(["git", "rev-parse", "--verify", revision + "^{commit}"], checkout)
        != revision
    ):
        raise ValueError(
            "Source revision unavailable in the configured existing checkout"
        )
    paths = config["files"]
    automation_ids = config["automation_ids"]
    if (
        not isinstance(automation_ids, list)
        or not 1 <= len(automation_ids) <= 20
        or any(not isinstance(value, str) or not value for value in automation_ids)
    ):
        raise ValueError("Configure the automation IDs authorized for this source")
    if not isinstance(paths, list) or not 1 <= len(paths) <= 30:
        raise ValueError("Configure one to thirty permitted source files")
    files = {}
    for path in paths:
        if (
            not isinstance(path, str)
            or not re.fullmatch(r"[\w./-]+", path)
            or Path(path).is_absolute()
            or ".." in Path(path).parts
            or path.startswith("-")
        ):
            raise ValueError("Unsafe configured source path")
        entry = command(["git", "ls-tree", revision, "--", path], checkout).split()
        mode = entry[0] if entry else None
        if mode not in ("100644", "100755"):
            raise ValueError("Source must be a regular tracked file")
        files[path] = command(
            ["git", "show", revision + ":" + path], checkout, strip=False
        )
    if sum(len(text.encode()) for text in files.values()) > 250000:
        raise ValueError("Configured source exceeds 250 KB")
    return {
        "repository": repo,
        "revision": revision,
        "files": files,
        "automation_ids": automation_ids,
        "executed_revision_verified": False,
    }


def run_checks(directory, checks, output, label):
    results = []
    safe_env = {
        key: value
        for key, value in os.environ.items()
        if key in ("PATH", "LANG", "LC_ALL", "TMPDIR", "VIRTUAL_ENV")
    }
    for index, check in enumerate(checks):
        argv = check["argv"]
        if (
            not isinstance(argv, list)
            or not argv
            or any(not isinstance(item, str) for item in argv)
        ):
            raise ValueError("Validation commands must be configured argv arrays")
        path = output / f"{label}-{index}.log"
        with path.open("x") as stream:
            path.chmod(0o600)
            try:
                process = subprocess.run(
                    argv,
                    cwd=directory,
                    env=safe_env,
                    stdout=stream,
                    stderr=subprocess.STDOUT,
                    timeout=120,
                )
                code = process.returncode
            except subprocess.TimeoutExpired:
                code = 124
        with path.open(errors="replace") as stream:
            excerpt = stream.read(8000)
        results.append(
            {
                "name": check["name"],
                "argv": argv,
                "exit_code": code,
                "log": path.name,
                "excerpt": redact(excerpt),
            }
        )
    return results


def propose_fix(args, report, snapshot):
    config = read_json(args.source_config)
    source = load_source(args.source_config)
    if source != snapshot["source"]:
        raise ValueError("Source changed since it was frozen")
    proposal = report["proposals"][0]
    findings = [
        finding
        for finding in report["findings"]
        if finding["id"] in proposal["finding_ids"]
    ]
    if any(finding["cause"] != "workflow" for finding in findings):
        raise ValueError("Only workflow findings may produce source-change PRs")
    if any(
        finding["automation_id"] not in source["automation_ids"] for finding in findings
    ):
        raise ValueError("Finding automation is outside the configured source scope")
    unevaluated = getattr(args, "publish_draft", False) or getattr(
        args, "check_draft", False
    )
    publishing = args.publish_fix or getattr(args, "publish_draft", False)
    checks = [] if unevaluated else config["checks"]
    if not unevaluated and (
        not isinstance(checks, list)
        or not 2 <= len(checks) <= 4
        or not any(check.get("baseline") == "fail" for check in checks)
        or not any(check.get("baseline") == "pass" for check in checks)
    ):
        raise ValueError(
            "Configure two to four checks, including a baseline-failing regression and a passing control"
        )
    if any(check.get("baseline") not in ("pass", "fail") for check in checks):
        raise ValueError("Every check needs an expected baseline outcome")
    fingerprint = hashlib.sha256(
        "|".join(sorted(finding["fingerprint"] for finding in findings)).encode()
    ).hexdigest()[:20]
    publication_automation = getattr(args, "publication_automation_id", None)
    publication_week = getattr(args, "publication_week", None)
    if publication_automation or publication_week:
        if (
            not publication_automation
            or not re.fullmatch(r"[0-9]{4}-W[0-9]{2}", publication_week or "")
            or {finding["automation_id"] for finding in findings}
            != {publication_automation}
        ):
            raise ValueError(
                "Weekly publication must be scoped to exactly one automation"
            )
        fingerprint = hashlib.sha256(
            (publication_automation + "|" + publication_week).encode()
        ).hexdigest()[:20]
    branch = "automation/improve-" + fingerprint
    if publishing:
        current_base = command(
            [
                "gh",
                "api",
                f"repos/{source['repository']}/git/ref/heads/{config['base_branch']}",
                "--jq",
                ".object.sha",
            ]
        )
        if current_base != source["revision"]:
            raise ValueError(
                "Configured baseline is no longer the publication base; collect again"
            )
        existing = json.loads(
            command(
                [
                    "gh",
                    "pr",
                    "list",
                    "--repo",
                    source["repository"],
                    "--head",
                    branch,
                    "--base",
                    config["base_branch"],
                    "--state",
                    "all",
                    "--json",
                    "url",
                ]
            )
        )
        if existing:
            return {"status": "already_tracked", "pr_url": existing[0]["url"]}
    checkout = Path(config["checkout"]).expanduser().resolve()
    baseline = (args.output / "baseline").resolve()
    candidate = (args.output / "candidate").resolve()
    if not unevaluated:
        command(
            ["git", "worktree", "add", "--detach", str(baseline), source["revision"]],
            checkout,
        )
    command(
        ["git", "worktree", "add", "--detach", str(candidate), source["revision"]],
        checkout,
    )
    patch = args.output / "candidate.patch"
    patch.write_text(proposal["patch"])
    patch.chmod(0o600)
    command(["git", "apply", "--index", "--check", str(patch.resolve())], candidate)
    command(["git", "apply", "--index", str(patch.resolve())], candidate)
    changed = command(
        ["git", "diff", "--cached", "--name-only"], candidate
    ).splitlines()
    if not changed or not set(changed) <= set(source["files"]):
        raise ValueError("Patch touches files outside the configured allowlist")
    for name in changed:
        if (candidate / name).is_symlink() or not (candidate / name).is_file():
            raise ValueError("Patch must preserve regular source files")
        if name.endswith(".py"):
            try:
                ast.parse((candidate / name).read_bytes(), filename=name)
            except SyntaxError as error:
                raise ValueError(
                    "Patch produces invalid Python syntax: " + name
                ) from error
    command(["git", "diff", "--cached", "--check"], candidate)
    expected_diff = command(["git", "diff", "HEAD", "--binary"], candidate, strip=False)
    before = (
        [] if unevaluated else run_checks(baseline, checks, args.output, "baseline")
    )
    after = (
        [] if unevaluated else run_checks(candidate, checks, args.output, "candidate")
    )
    if (
        command(["git", "diff", "HEAD", "--binary"], candidate, strip=False)
        != expected_diff
    ):
        raise ValueError("Validation commands modified the candidate source")
    results = {
        "source_revision": source["revision"],
        "changed_files": changed,
        "baseline": before,
        "candidate": after,
        "status": "rejected",
    }
    for check, first, second in zip(checks, before, after, strict=True):
        if (
            (first["exit_code"] == 0) != (check["baseline"] == "pass")
            or second["exit_code"] != 0
            or first["exit_code"] == 124
        ):
            write_json(args.output / "validation.json", results)
            return results
    results["status"] = (
        "unevaluated_draft" if unevaluated else "checks_passed_not_task_quality_proven"
    )
    if unevaluated and not publishing:
        results["status"] = "mechanical_checks_passed"
    results["behavioral_evaluation"] = (
        "not_performed" if unevaluated else "configured_checks_only"
    )
    results["mechanical_checks"] = [
        "git apply --check",
        "git diff --check",
        "source allowlist",
        "Python syntax for changed .py files",
    ]
    write_json(args.output / "validation.json", results)
    if not publishing:
        return results
    remote = command(["git", "remote", "get-url", "origin"], checkout)
    if remote not in (
        f"https://github.com/{source['repository']}.git",
        f"git@github.com:{source['repository']}.git",
        f"https://github.com/{source['repository']}",
    ):
        raise ValueError(
            "Configured checkout origin differs from the publication repository"
        )
    command(["git", "add", "--", *changed], candidate)
    command(["git", "commit", "-m", proposal["title"][:120]], candidate)
    if (
        command(
            ["git", "diff", source["revision"], "HEAD", "--binary"],
            candidate,
            strip=False,
        )
        != expected_diff
    ):
        raise ValueError("Commit differs from the validated patch")
    results["candidate_revision"] = command(["git", "rev-parse", "HEAD"], candidate)
    if command(
        ["git", "ls-remote", "--heads", "origin", "refs/heads/" + branch], candidate
    ):
        raise ValueError(
            "Improvement branch already exists without a tracked PR; inspect before retrying"
        )
    command(["git", "push", "origin", "HEAD:refs/heads/" + branch], candidate)
    links = sorted({url for finding in findings for url in finding["trace_urls"]})
    body = (
        proposal["hypothesis"]
        + "\n\nTrace evidence:\n"
        + "\n".join("- " + value for value in links)
        + f"\n\nBaseline revision: {source['revision']}. Executed automation revision has not been verified.\n\n"
        + proposal["evaluation_plan"]
        + (
            "\n\n**UNEVALUATED PROPOSAL:** behavioral evaluation has not been performed. "
            "The checks below only establish that the patch applies within the approved source scope.\n"
            if unevaluated
            else "\n\nExecuted configured checks:\n"
        )
        + (
            "\nMechanical checks: patch application, whitespace integrity, Python syntax, and approved file scope passed.\n"
            if unevaluated
            else ""
        )
        + "\n".join(
            f"- {first['name']}: baseline exit {first['exit_code']}; candidate exit {second['exit_code']}"
            for first, second in zip(before, after, strict=True)
        )
        + "\n\nThese checks do not establish general task-quality improvement. Human review controls merge and deployment. "
        "Recovery: retain or restore the baseline revision through the normal reviewed release process.\n\n"
        "Created by OpenHands on behalf of the user."
    )
    markers = [
        "<!-- automation-improvement-event: " + finding["signal_event_id"] + " -->"
        for finding in findings
        if finding.get("signal_event_id")
    ]
    if publication_automation:
        markers.extend(
            [
                "<!-- automation-improvement-automation: "
                + publication_automation
                + " -->",
                "<!-- automation-improvement-week: " + publication_week + " -->",
            ]
        )
    if report.get("aggregation"):
        aggregation = report["aggregation"]
        body += f"\n\nAutomation backlog reviewed: {aggregation['total_events']} findings across {aggregation['affected_conversations']} distinct conversations. These counts are not validated failure rates.\n"
    body = "\n".join(markers) + "\n\n" + body
    body_path = args.output / "pr-body.md"
    body_path.write_text(redact(body))
    body_path.chmod(0o600)
    results["pr_url"] = command(
        [
            "gh",
            "pr",
            "create",
            "--repo",
            source["repository"],
            "--base",
            config["base_branch"],
            "--head",
            branch,
            "--draft",
            "--title",
            (("[Unevaluated] " if unevaluated else "") + proposal["title"])[:120],
            "--body-file",
            str(body_path.resolve()),
        ],
        candidate,
    )
    return results
