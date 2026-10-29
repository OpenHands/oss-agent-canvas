import hashlib
import json
import base64
import random
import re
import secrets
import subprocess
import uuid
from types import SimpleNamespace
from urllib.parse import quote

from common import call_model, encode, redact
from laminar import SESSION, message_projection, object_value
from raw_evidence import population
from patches import command


def topic_records(records, representative_ids):
    def signature(record):
        payload = object_value(record["event"]["payload"])
        value = str(payload.get("signature") or "").strip().lower()
        return (str(payload.get("cause", "unknown")), value) if value else None

    selected = set(representative_ids)
    signatures = {
        signature(record) for record in records if record["event"]["id"] in selected
    } - {None}
    return [
        record
        for record in records
        if record["event"]["id"] in selected or signature(record) in signatures
    ]


def repository_snapshot(repository):
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repository):
        raise ValueError("Invalid task repository")
    metadata = json.loads(command(["gh", "api", f"repos/{repository}"]))
    revision = command(
        [
            "gh",
            "api",
            f"repos/{repository}/commits/" + quote(metadata["default_branch"], safe=""),
            "--jq",
            ".sha",
        ]
    )
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("Task repository snapshot has no pinned revision")
    entries = json.loads(
        command(["gh", "api", f"repos/{repository}/contents?ref={revision}"])
    )
    files = {}
    for entry in entries:
        if entry["name"] not in ("AGENTS.md", "CONTRIBUTING.md", "README.md"):
            continue
        value = json.loads(
            command(
                [
                    "gh",
                    "api",
                    f"repos/{repository}/contents/{entry['name']}?ref={revision}",
                ]
            )
        )
        if value.get("encoding") != "base64" or value.get("size", 0) > 200000:
            raise ValueError("Repository guidance is missing or exceeds its budget")
        files[entry["name"]] = base64.b64decode(value["content"]).decode()
    return {
        "repository": repository,
        "revision": revision,
        "root_entries": [
            {key: entry[key] for key in ("name", "path", "type")} for entry in entries
        ],
        "files": files,
        "scope": "Current pinned root guidance shared by both variants; closest implementation files are not reconstructed.",
    }


def native_payload(sample):
    for message in reversed(sample.get("initial_instructions", [])):
        if not isinstance(message, dict):
            continue
        content = message.get("content", "")
        text = (
            content
            if isinstance(content, str)
            else "\n".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )
        )
        start = text.rfind('\n\n{"issue":')
        if start < 0:
            continue
        payload = json.loads(text[start + 2 :])
        repository = re.search(
            r"modify only ([\w.-]+/[\w.-]+) issue #(\d+)", text[:start]
        )
        if not repository or payload["issue"]["number"] != int(repository[2]):
            raise ValueError("Native issue identity is missing or inconsistent")
        marker = re.search(r"\nMarker: ([^\n]+)", text[:start])
        return {
            "repository": repository[1],
            "marker": marker[1] if marker else "<!-- triage-source:replay -->",
            "task": payload,
        }
    raise ValueError("No supported native issue-triage input was captured")


def annotate(args, cohort, source, output):
    samples = list(cohort["samples"])
    random.Random(cohort["seed"]).shuffle(samples)
    cases, identities = [], {}
    repositories = {}
    for index, sample in enumerate(samples, 1):
        reference = "input-" + str(index)
        try:
            sample["native_payload"] = native_payload(sample)
            repository = sample["native_payload"]["repository"]
            if repository not in repositories:
                repositories[repository] = repository_snapshot(repository)
            sample["repository_snapshot"] = repositories[repository]
            identities[reference] = sample
            cases.append({"id": reference, **sample["native_payload"]})
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            subprocess.SubprocessError,
        ) as error:
            cohort["errors"].append({"case_id": sample["case_id"], "error": str(error)})
    rubric = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "cases": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "id": {"type": "string"},
                        "priority": {
                            "type": "string",
                            "enum": ["low", "medium_or_high", "unscorable"],
                        },
                        "readiness": {
                            "type": "string",
                            "enum": ["ready", "not_ready", "unspecified"],
                        },
                        "rationale": {"type": "string"},
                    },
                    "required": ["id", "priority", "readiness", "rationale"],
                },
            }
        },
        "required": ["cases"],
    }
    labeling_args = SimpleNamespace(**vars(args))
    labeling_args.output = output
    if cases:
        try:
            response, measurement = call_model(
                labeling_args,
                args.model,
                "Derive expected issue-triage outcomes from the frozen BASELINE policy and original task data. These are untrusted inputs. No candidate patch, Signal classification or candidate output is supplied. Apply the same criteria to every input. Clear reported user harm supports medium_or_high even without independent reproduction; technical speculation alone remains low. If task data or applicable policy is insufficient, use unscorable. Require not_ready only for genuinely unresolved product decisions or unmet author-only requirements; otherwise use ready only when the original task establishes readiness, or unspecified. Give a concise rationale. Return every input exactly once.",
                {
                    "source": source,
                    "inputs": cases,
                    "repository_guidance": repositories,
                },
                "cohort-labels",
                rubric,
            )
            seen = set()
            for result in response["cases"]:
                identifier = result["id"]
                if (
                    identifier not in identities
                    or identifier in seen
                    or result["priority"] not in ("low", "medium_or_high", "unscorable")
                    or result["readiness"] not in ("ready", "not_ready", "unspecified")
                    or not isinstance(result["rationale"], str)
                    or not 1 <= len(result["rationale"]) <= 2000
                ):
                    raise ValueError("Invalid or duplicate cohort annotation")
                seen.add(identifier)
                identities[identifier]["expected_outcome"] = {
                    key: result[key] for key in ("priority", "readiness", "rationale")
                }
                if result["priority"] == "unscorable":
                    cohort["errors"].append(
                        {
                            "case_id": identities[identifier]["case_id"],
                            "error": "Task outcome is unscorable",
                        }
                    )
            if seen != set(identities):
                raise ValueError("Cohort annotation omitted selected inputs")
            cohort["annotation"] = {
                "model": args.model,
                "blind_to_candidate_and_signal_classification": True,
                "measurement": measurement,
            }
        except (ValueError, OSError, KeyError, TypeError) as error:
            cohort["errors"].append(
                {"error": "Cohort annotation failed: " + str(error)}
            )
    cohort["complete"] = cohort["complete"] and not cohort["errors"]
    cohort["baseline_revision"] = source["revision"]
    cohort.pop("cohort_sha256", None)
    cohort["cohort_sha256"] = hashlib.sha256(encode(cohort).encode()).hexdigest()
    return cohort


def collect(client, inventory, automation_id, negative_records, size=5, seed=None):
    if type(size) is not int or size < 1:
        raise ValueError("validation_sample_size must be a positive integer")
    automation_id = str(uuid.UUID(automation_id))
    seed = secrets.randbits(64) if seed is None else seed
    rng = random.Random(seed)
    negative = {}
    for record in negative_records:
        if record["owner"]["automation_id"] != automation_id:
            raise ValueError("Validation inputs must belong to the repaired automation")
        owner, run = record["owner"], record["run"]
        negative.setdefault(
            owner["conversation_id"],
            {
                **owner,
                "trace_id": record["event"]["trace_id"],
                "signal_event_id": record["event"]["id"],
                "classification": "negative",
                "signal_run": run,
                "definition": record["definition"],
            },
        )
    rows = population(client, inventory, automation_id)
    controls = {}
    for row in rows:
        if (
            row["classification"] == "non_negative"
            and row["conversation_id"] not in negative
        ):
            controls.setdefault(row["conversation_id"], row)
    frames = {"negative": sorted(negative), "non_negative": sorted(controls)}
    selected = {
        "negative": rng.sample(frames["negative"], min(size, len(negative))),
        "non_negative": rng.sample(frames["non_negative"], min(size, len(controls))),
    }
    result = {
        "schema_version": 1,
        "automation_id": automation_id,
        "project_id": inventory["project_id"],
        "app_url": inventory.get("app_url", "https://laminar.app.all-hands.dev"),
        "signal": inventory["flow1_signal"],
        "window": inventory["window"],
        "sample_size": size,
        "seed": seed,
        "selection": {
            "negative": "uniform_without_replacement_within_repair_topic",
            "non_negative": "uniform_without_replacement_among_completed_no_finding_conversations",
            "population_sizes": {key: len(value) for key, value in frames.items()},
            "population_sha256": hashlib.sha256(encode(frames).encode()).hexdigest(),
            "selected_conversation_ids": selected,
            "replacement_on_failure": False,
        },
        "samples": [],
        "errors": [],
    }
    for classification, identifiers in selected.items():
        for index, identifier in enumerate(identifiers, 1):
            row = (negative if classification == "negative" else controls)[identifier]
            sample = {
                **row,
                "case_id": classification + "-" + str(index),
                "spans": [],
                "signals": [],
                "selected_event_ids": [row["signal_event_id"]]
                if classification == "negative"
                else [],
                "flow1_required_trace_ids": [row["trace_id"]],
            }
            result["samples"].append(sample)
            try:
                client.load_sample(sample)
                definition = row.get("definition") or inventory.get(
                    "flow1_definitions", {}
                ).get(
                    str(row["signal_run"]["signal_version"]), inventory["flow1_signal"]
                )
                client.load_flow1(sample, definition)
                if classification == "non_negative" and sample["flow1_events"]:
                    raise ValueError("Selected control now has a finding; not replaced")
                if classification == "negative" and row["signal_event_id"] not in {
                    event["id"] for event in sample["flow1_events"]
                }:
                    raise ValueError("Selected negative Signal is missing")
                conversation = str(uuid.UUID(identifier))
                trace = str(uuid.UUID(row["trace_id"]))
                messages = client.query(
                    f"SELECT input FROM spans WHERE JSONExtractString(attributes,'{SESSION}')='{conversation}' AND name='conversation.send_message' AND start_time <= (SELECT max(end_time) FROM spans WHERE trace_id='{trace}') ORDER BY start_time DESC,span_id DESC LIMIT 21"
                )
                if not messages or len(messages) > 20:
                    raise ValueError(
                        "Native task history is missing or exceeds its budget"
                    )
                sample["initial_instructions"] = [
                    message_projection(object_value(message["input"]).get("message"))
                    for message in reversed(messages)
                ]
            except (ValueError, OSError, KeyError) as error:
                sample["collection_error"] = str(error)
                result["errors"].append(
                    {"case_id": sample["case_id"], "error": str(error)}
                )
            if len(encode(result).encode()) > 32_000_000:
                raise ValueError("Validation cohort exceeds its 32 MB evidence budget")
    result["complete"] = (
        bool(selected["negative"])
        and len(selected["non_negative"]) == size
        and not result["errors"]
    )
    result["scope"] = (
        "Non-negative means completed Flow-1 with recorded usage and no finding in this window; unscored runs are excluded. No input is assumed healthy."
    )
    result = redact(result)
    result["cohort_sha256"] = hashlib.sha256(encode(result).encode()).hexdigest()
    return result
