import argparse
import fcntl
import hashlib
import json
import os
import re
import signal
import subprocess
from pathlib import Path

from common import read_json, redact, write_json

START = "<!-- automation-comparison:start -->"
END = "<!-- automation-comparison:end -->"
STATUSES = {"improvement_on_development_cases", "regressed", "inconclusive"}


def section(text):
    return f"{START}\n## Before / After Evidence\n\n{text.strip()}\n{END}"


def replace_section(body, text):
    if START not in body and END not in body:
        return body.rstrip() + "\n\n" + section(text) + "\n"
    if body.count(START) != 1 or body.count(END) != 1:
        raise ValueError("Ambiguous comparison section")
    first, last = body.index(START), body.index(END)
    if first >= last:
        raise ValueError("Invalid comparison section")
    return body[:first] + section(text) + body[last + len(END) :]


def execute(argv, log, timeout):
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in ("PATH", "HOME", "LANG", "LC_ALL", "LITELLM_API_KEY")
    }
    if os.environ.get("LITELLM_API_KEY"):
        environment["VALIDATION_JUDGE_API_KEY"] = os.environ["LITELLM_API_KEY"]
    if os.environ.get("COMPARISON_API_KEY"):
        environment["LITELLM_API_KEY"] = os.environ["COMPARISON_API_KEY"]
    with log.open("x") as stream:
        log.chmod(0o600)
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=stream,
            stderr=subprocess.STDOUT,
            env=environment,
            start_new_session=True,
        )
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGINT)
            try:
                process.wait(timeout=60)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            raise ValueError("Comparison timed out; retained as inconclusive") from None
        if code:
            raise ValueError("Comparison runner failed; inspect its private log")


def validate_receipt(receipt, request):
    if (
        receipt.get("baseline") != request["baseline"]
        or receipt.get("candidate") != request["candidate"]
        or receipt.get("status") not in STATUSES
        or not isinstance(receipt.get("markdown"), str)
        or not 1 <= len(receipt["markdown"]) <= 40000
        or START in receipt["markdown"]
        or END in receipt["markdown"]
    ):
        raise ValueError("Comparison receipt does not match the repair revisions")


def evaluate(request_path):
    request = read_json(request_path)
    root = request_path.parent
    output = root / "comparison"
    receipt_path = output / "evidence.json"
    plan = request["plan"]
    argv, timeout = plan["command"], plan.get("timeout_seconds", 1800)
    if (
        not isinstance(argv, list)
        or not argv
        or any(not isinstance(part, str) or not part for part in argv)
        or not Path(argv[0]).is_absolute()
        or type(timeout) is not int
        or not 30 <= timeout <= 7200
    ):
        raise ValueError("Configure a trusted comparison command and bounded timeout")
    try:
        failure_path = root / "comparison-failure.json"
        if failure_path.exists():
            raise ValueError(
                "Previous comparison failed; retain it rather than rerolling"
            )
        if not receipt_path.exists():
            execute(
                [
                    *argv,
                    "--request",
                    str(request_path.resolve()),
                    "--output",
                    str(output),
                ],
                root / "comparison-runner.log",
                timeout,
            )
        receipt = read_json(receipt_path)
        validate_receipt(receipt, request)
        if (
            receipt.get("request_sha256")
            != hashlib.sha256(request_path.read_bytes()).hexdigest()
        ):
            raise ValueError("Comparison receipt does not match the frozen request")
    except (OSError, ValueError) as error:
        receipt = {
            "baseline": request["baseline"],
            "candidate": request["candidate"],
            "status": "inconclusive",
            "markdown": "**Inconclusive:** the comparison did not produce valid evidence. "
            "The repair remains an unevaluated draft. See the private comparison log.",
            "error": redact(str(error)),
        }
        if not failure_path.exists():
            write_json(failure_path, receipt)
    return receipt


def run_locked(request_path, command):
    request = read_json(request_path)
    if not request.get("pr_url"):
        request["pr_url"] = read_json(
            request_path.parent / "comparison-publication.json"
        )["pr_url"]
    match = re.fullmatch(
        r"https://github.com/([\w.-]+/[\w.-]+)/pull/([1-9][0-9]*)", request["pr_url"]
    )
    if not match or match[1] != request["repository"]:
        raise ValueError("Comparison PR must belong to the source repository")
    endpoint = f"repos/{match[1]}/pulls/{match[2]}"
    root = request_path.parent
    receipt_path = root / "comparison/evidence.json"
    receipt = evaluate(request_path)
    current = json.loads(command(["gh", "api", endpoint]))
    if current["head"]["sha"] != request["candidate"] or current["state"] != "open":
        return {"status": "stale_pr", "comparison_status": receipt["status"]}
    body = replace_section(current["body"], redact(receipt["markdown"]))
    payload = {"body": body}
    supported = receipt["status"] == "improvement_on_development_cases"
    body = (
        body.replace(
            "**Development comparison recorded:** see Before / After Evidence for executed checks, exact source revisions and limitations.",
            "**Development comparison recorded:** see Before / After Evidence for executed checks, exact source revisions and limitations."
            if supported
            else f"**Comparison status: {receipt['status']}.** Improvement has not been demonstrated; see Before / After Evidence.",
        )
        .replace(
            "**UNEVALUATED PROPOSAL:** behavioral evaluation has not been performed. The checks below only establish that the patch applies within the approved source scope.",
            "**Development comparison recorded:** see Before / After Evidence for executed checks, exact source revisions and limitations."
            if supported
            else f"**Comparison status: {receipt['status']}.** Improvement has not been demonstrated; see Before / After Evidence.",
        )
        .replace(
            "This proposal has not been validated.",
            "The original plan is superseded by the recorded comparison below.",
        )
    )
    payload["body"] = body
    for previous in (
        "[Unevaluated] ",
        "[Evidence] ",
        "[Inconclusive] ",
        "[Regressed] ",
        "[Observed regression] ",
        "[Mechanism tested] ",
    ):
        if current.get("title", "").startswith(previous):
            prefix = (
                "Evidence"
                if supported
                else "Observed regression"
                if receipt["status"] == "regressed"
                else "Mechanism tested"
                if (receipt.get("assessment") or {}).get("mechanism_supported")
                else receipt["status"].capitalize()
            )
            payload["title"] = f"[{prefix}] " + current["title"][len(previous) :]
            break
    body_path = root / "pr-body-with-evidence.md"
    body_path.write_text(body)
    body_path.chmod(0o600)
    payload_path = root / "pr-evidence-update.json"
    payload_path.write_text(json.dumps(payload))
    payload_path.chmod(0o600)
    command(["gh", "api", "--method", "PATCH", endpoint, "--input", str(payload_path)])
    if not supported and current.get("draft") is False:
        command(["gh", "pr", "ready", request["pr_url"], "--undo"])
    return {
        "status": receipt["status"],
        "published": True,
        "receipt": str(receipt_path),
    }


def run(request_path, command):
    with (request_path.parent / ".comparison.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return run_locked(request_path, command)


def after_publication(config, source, snapshot, results, output, command):
    plan = config.get("comparative_validation")
    if not plan:
        return {"status": "not_configured", "published": False}
    path = output / "comparison-request.json"
    if path.exists():
        frozen = read_json(path)
        if (
            frozen["baseline"] != source["revision"]
            or frozen["candidate"] != results["candidate_revision"]
        ):
            raise ValueError("Published source differs from the evaluated candidate")
        link_path = output / "comparison-publication.json"
        if link_path.exists():
            if read_json(link_path)["pr_url"] != results["pr_url"]:
                raise ValueError("Comparison is already linked to another PR")
        else:
            write_json(link_path, {"pr_url": results["pr_url"]})
        return run(path, command)
    snapshot_path = output / "comparison-snapshot.json"
    write_json(snapshot_path, {"samples": snapshot.get("samples", [])})
    request = {
        "schema_version": 1,
        "repository": source["repository"],
        "checkout": config["checkout"],
        "pr_url": results["pr_url"],
        "baseline": source["revision"],
        "candidate": results["candidate_revision"],
        "changed_files": results["changed_files"],
        "topic": snapshot.get("topic"),
        "repair_issue": snapshot.get("repair_issue"),
        "findings": snapshot.get("comparison_findings", []),
        "snapshot": str(snapshot_path.resolve()),
        "cohort": snapshot.get("validation_cohort"),
        "trace_urls": sorted(
            {
                url
                for finding in snapshot.get("comparison_findings", [])
                for url in finding["trace_urls"]
            }
        ),
        "plan": plan,
    }
    write_json(path, request)
    try:
        return run(path, command)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        return {
            "status": "inconclusive",
            "published": False,
            "error": redact(str(error)),
        }


def before_publication(config, source, snapshot, results, proposal, findings, output):
    plan = config.get("comparative_validation")
    if not plan:
        return {
            "status": "inconclusive",
            "markdown": "No comparison runner configured.",
        }
    cohort = snapshot.get("validation_cohort")
    if "validation_sample_size" in config and (
        not cohort or cohort["sample_size"] != config["validation_sample_size"]
    ):
        return {
            "status": "inconclusive",
            "markdown": "The configured historical validation cohort is missing or has the wrong N.",
        }
    snapshot_path = output / "comparison-snapshot.json"
    write_json(snapshot_path, {"samples": snapshot.get("samples", [])})
    request = {
        "schema_version": 1,
        "repository": source["repository"],
        "checkout": config["checkout"],
        "baseline": source["revision"],
        "candidate": results["candidate_revision"],
        "changed_files": results["changed_files"],
        "topic": snapshot.get("topic"),
        "repair_issue": snapshot.get("repair_issue"),
        "findings": findings,
        "hypothesis": proposal["hypothesis"],
        "snapshot": str(snapshot_path.resolve()),
        "cohort": cohort,
        "plan": plan,
        "development_feedback_used": bool(snapshot.get("repair_feedback")),
    }
    path = output / "comparison-request.json"
    write_json(path, request)
    return evaluate(path)


if __name__ == "__main__":
    from patches import command

    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True, type=Path)
    print(json.dumps(run(parser.parse_args().request.resolve(), command)))
