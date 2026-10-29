import hashlib
import json
import random
import re
import secrets
import subprocess
import uuid

from common import encode, redact
from laminar import SESSION, message_projection, object_value
from raw_evidence import population
from repository_snapshot import capture as repository_snapshot, capture_reads


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
        if not marker or "[REDACTED]" in marker[1]:
            raise ValueError("Native task marker is missing or redacted")
        return {
            "repository": repository[1],
            "marker": marker[1],
            "task": payload,
        }
    raise ValueError("No supported native issue-triage input was captured")


def prepare(cohort, source, output):
    cohort.pop("annotation", None)
    repositories, response_cache = {}, {}
    for sample in cohort["samples"]:
        try:
            sample.pop("expected_outcome", None)
            sample["native_payload"] = native_payload(sample)
            repository = sample["native_payload"]["repository"]
            if repository not in repositories:
                repositories[repository] = repository_snapshot(
                    repository, output / repository.replace("/", "_")
                )
            sample["repository_snapshot"] = repositories[repository]
            sample["github_reads"] = capture_reads(
                sample["native_payload"], response_cache
            )
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            subprocess.SubprocessError,
        ) as error:
            cohort["errors"].append({"case_id": sample["case_id"], "error": str(error)})
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
