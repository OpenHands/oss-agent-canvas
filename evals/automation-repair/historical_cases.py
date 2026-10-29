import hashlib
import json
import re
import uuid
from pathlib import Path


def load(metadata, contract):
    path = Path(metadata["path"])
    if path.stat().st_size > 32_000_000:
        raise ValueError("Historical cohort exceeds its evidence budget")
    cohort = json.loads(path.read_text())
    expected = cohort.pop("cohort_sha256")
    digest = hashlib.sha256(
        json.dumps(cohort, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    if digest != expected or digest != metadata["sha256"]:
        raise ValueError("Historical cohort changed after selection")
    if contract != "issue_priority":
        raise ValueError(
            "Historical execution adapter is not available for this repair family"
        )
    size = cohort["sample_size"]
    if type(size) is not int or size < 1 or size != metadata["sample_size"]:
        raise ValueError("Invalid historical sample size")
    negative = [
        sample for sample in cohort["samples"] if sample["classification"] == "negative"
    ]
    controls = [
        sample
        for sample in cohort["samples"]
        if sample["classification"] == "non_negative"
    ]
    if (
        not cohort["complete"]
        or cohort["errors"]
        or not 1 <= len(negative) <= size
        or len(controls) != size
    ):
        raise ValueError(
            "Historical cohort is incomplete: require up to N negative inputs and N non-negative inputs"
        )
    identities = [sample["conversation_id"] for sample in cohort["samples"]]
    traces = [sample["trace_id"] for sample in cohort["samples"]]
    if (
        len(identities) != len(set(identities))
        or len(negative) + len(controls) != len(identities)
        or len(traces) != len(set(traces))
    ):
        raise ValueError("Historical validation inputs must be distinct")
    cases = []
    case_ids = set()
    for sample in cohort["samples"]:
        if (
            not re.fullmatch(
                r"(?:negative|non_negative)-[1-9][0-9]*", sample["case_id"]
            )
            or sample["case_id"] in case_ids
        ):
            raise ValueError("Historical case identities are invalid or duplicated")
        case_ids.add(sample["case_id"])
        uuid.UUID(sample["trace_id"])
        if sample["automation_id"] != cohort["automation_id"] or sample.get(
            "collection_error"
        ):
            raise ValueError("Historical input ownership or collection failed")
        native = sample["native_payload"]
        outcome = sample["expected_outcome"]
        if outcome["priority"] not in ("low", "medium_or_high"):
            raise ValueError("Historical input has no independent expected outcome")
        if outcome["readiness"] not in ("ready", "not_ready", "unspecified"):
            raise ValueError("Invalid historical readiness expectation")
        snapshot = sample.get("repository_snapshot")
        if not snapshot or not re.fullmatch(
            r"[0-9a-f]{40}", snapshot.get("revision", "")
        ):
            raise ValueError("Historical input lacks pinned repository guidance")
        kind = "regression" if sample["classification"] == "negative" else "control"
        cases.append(
            {
                "id": sample["case_id"],
                "family": "triage",
                "kind": kind,
                "body": native["task"]["issue"]["author_body"],
                "title": native["task"]["issue"]["title"],
                "native_payload": native,
                "repository_snapshot": sample.get(
                    "repository_snapshot", {"files": {}, "root_entries": []}
                ),
                "expected_priorities": ["priority:low"]
                if outcome["priority"] == "low"
                else ["priority:medium", "priority:high"],
                "readiness_expectation": outcome["readiness"],
                "expectation": "historical_task_outcome",
                "target_violation": "incorrect_priority"
                if kind == "regression"
                else None,
                "provenance": {
                    key: sample.get(key)
                    for key in (
                        "classification",
                        "conversation_id",
                        "trace_id",
                        "signal_event_id",
                    )
                },
                "cohort_sha256": digest,
                "historical_sample_size": size,
                "trace_url": cohort.get(
                    "app_url", "https://laminar.app.all-hands.dev"
                ).rstrip("/")
                + "/project/"
                + cohort["project_id"]
                + "/traces/"
                + sample["trace_id"],
            }
        )
    return cases
