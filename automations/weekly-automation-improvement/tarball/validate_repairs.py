import argparse
import hashlib
import json
import re
import uuid
from pathlib import Path

from common import read_json, redact, write_json
from evidence import after_publication, run
from laminar import Laminar, META, object_value
from patches import command, load_source
from sources import github_auth
from weekly import arguments, load_environment


def historical_cohort(config, body, revision, files, output):
    from cohorts import annotate, collect
    from events import collect_events
    from triage import group_findings

    owner = re.search(
        r"<!-- automation-improvement-automation: ([0-9a-f-]{36}) -->", body
    )
    if not owner:
        raise ValueError("Repair has no automation ownership marker")
    size = config.get("validation_sample_size", 5)
    path = output / "cohort.json"
    if path.exists():
        value = read_json(path, 32_000_000)
        if value["sample_size"] != size:
            raise ValueError("Cached cohort used a different N")
        if value.get("baseline_revision") != revision:
            raise ValueError("Cached cohort used a different baseline")
    else:
        args = arguments(config, output / "collection", 3, None)
        args.output.mkdir(parents=True, mode=0o700)
        client = Laminar(args)
        inventory = collect_events(client, set(), aggregate=True)
        marked = set(
            re.findall(r"<!-- automation-improvement-event: ([0-9a-f-]{36}) -->", body)
        )
        records = [
            record
            for record in inventory["events"]
            if record["owner"]["automation_id"] == owner[1]
        ]
        _, groups = group_findings(records)
        records = [
            record
            for members in groups.values()
            if any(record["event"]["id"] in marked for record in members)
            for record in members
        ]
        source_path = output / "source.json"
        write_json(
            source_path,
            {
                "repository": config["repository"],
                "checkout": config["checkout"],
                "revision": revision,
                "files": files,
                "automation_ids": [owner[1]],
            },
        )
        value = collect(client, inventory, owner[1], records, size=size)
        value = annotate(args, value, load_source(source_path), output / "labels")
        write_json(path, value)
    return {
        "path": str(path.resolve()),
        "sha256": value["cohort_sha256"],
        "sample_size": size,
        "complete": value["complete"],
    }


def collect_snapshot(config, body, output):
    marker = re.search(r"<!-- automation-improvement-automation: ([\w-]+) -->", body)
    if not marker:
        raise ValueError("Repair has no automation ownership marker")
    automation_id = str(uuid.UUID(marker[1]))
    traces = sorted(set(re.findall(r"/traces/([0-9a-f-]{36})", body)))
    if not traces:
        raise ValueError("Repair has no trace evidence")
    client = Laminar(arguments(config, output, 3, 0))
    samples = []
    for trace in traces:
        trace = str(uuid.UUID(trace))
        owners = client.query(
            f"SELECT DISTINCT JSONExtractString(attributes,'{META}automation.id') AS automation_id FROM spans WHERE trace_id='{trace}' AND JSONExtractString(attributes,'{META}automation.id') != ''"
        )
        if {row["automation_id"] for row in owners} != {automation_id}:
            raise ValueError("Trace does not belong to the repair automation")
        rows = client.query(
            f"SELECT input FROM spans WHERE trace_id='{trace}' AND name='conversation.send_message' ORDER BY start_time,span_id LIMIT 21"
        )
        if not rows or len(rows) > 20:
            raise ValueError("Trace has missing or oversized task inputs")
        samples.append(
            {
                "automation_id": automation_id,
                "trace_ids": [trace],
                "initial_instructions": [
                    object_value(row["input"]).get("message") for row in rows
                ],
            }
        )
    return redact({"samples": samples})


def validate(config, output, numbers=()):
    load_environment(config)
    github_auth(config)
    if not config.get("comparative_validation", {}).get("command"):
        raise ValueError("Configure a global comparison runner")
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    repository = config["repository"]
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repository):
        raise ValueError("Invalid repository")
    prs, page = [], 1
    while True:
        rows = json.loads(
            command(
                [
                    "gh",
                    "api",
                    f"repos/{repository}/pulls?state=open&per_page=100&page={page}",
                ]
            )
        )
        prs.extend(rows)
        if len(rows) < 100:
            break
        page += 1
    reports = []
    for item in prs:
        if numbers and item["number"] not in numbers:
            continue
        if not item["head"]["ref"].startswith("automation/improve-") or not any(
            label["name"] == "automation-improvement"
            for label in item.get("labels", [])
        ):
            continue
        pr = json.loads(
            command(["gh", "api", f"repos/{repository}/pulls/{item['number']}"])
        )
        plan = config["comparative_validation"]
        harness = Path(plan["command"][-1]).parent
        code = {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in harness.glob("*.py")
        }
        fingerprint = hashlib.sha256(
            json.dumps(
                [plan, code, config.get("validation_sample_size", 5)], sort_keys=True
            ).encode()
        ).hexdigest()[:12]
        directory = output / (
            str(item["number"]) + "-" + pr["head"]["sha"] + "-" + fingerprint
        )
        directory.mkdir(mode=0o700, exist_ok=True)
        try:
            request_path = directory / "comparison-request.json"
            if request_path.exists():
                result = run(request_path, command)
            else:
                body = pr["body"] or ""
                baseline = re.search(r"Baseline revision: ([0-9a-f]{40})", body)
                revision = baseline[1] if baseline else pr["base"]["sha"]
                command(["git", "check-ref-format", "--branch", pr["head"]["ref"]])
                command(
                    ["git", "fetch", "origin", pr["head"]["ref"]], config["checkout"]
                )
                files = json.loads(
                    command(
                        [
                            "gh",
                            "api",
                            f"repos/{repository}/pulls/{item['number']}/files?per_page=100",
                        ]
                    )
                )
                if len(files) > 30:
                    raise ValueError("Repair exceeds the approved source-file budget")
                files = [entry["filename"] for entry in files]
                if config.get("legacy_development_fixtures", False):
                    snapshot = collect_snapshot(config, body, directory)
                else:
                    cohort_output = (
                        output
                        / f"cohort-{item['number']}-{pr['head']['sha']}-N{config.get('validation_sample_size', 5)}"
                    )
                    cohort_output.mkdir(parents=True, exist_ok=True, mode=0o700)
                    snapshot = {
                        "samples": [],
                        "validation_cohort": historical_cohort(
                            config, body, revision, files, cohort_output
                        ),
                    }
                topic = re.search(
                    r"<!-- automation-improvement-topic-key: ([\w-]+) -->", body
                )
                snapshot["topic"] = {
                    "key": topic[1] if topic else "existing-repair",
                    "title": pr["title"],
                }
                snapshot["comparison_findings"] = [
                    {
                        "summary": body.split("\n\nTrace evidence:", 1)[0][:4000],
                        "trace_urls": [],
                    }
                ]
                evaluation_config = dict(config)
                recorded = config.get("recorded_runs_by_pr", {}).get(
                    str(item["number"])
                )
                if recorded:
                    evaluation_config["comparative_validation"] = config[
                        "comparative_validation"
                    ] | {"recorded_runs": recorded}
                result = after_publication(
                    evaluation_config,
                    {"repository": repository, "revision": revision},
                    snapshot,
                    {
                        "pr_url": pr["html_url"],
                        "candidate_revision": pr["head"]["sha"],
                        "changed_files": files,
                    },
                    directory,
                    command,
                )
            reports.append({"number": item["number"], **result})
        except Exception as error:
            reports.append(
                {
                    "number": item["number"],
                    "status": "inconclusive",
                    "error": redact(str(error))[:500],
                }
            )
    return reports


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--pr", type=int, action="append", default=[])
    args = parser.parse_args()
    print(
        json.dumps(
            validate(read_json(args.config), args.output.resolve(), set(args.pr))
        )
    )
