import hashlib
import json
import re

from common import encode, timestamp

SCHEMA = "openhands.findings.v1"
ISSUE = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/issues/[1-9][0-9]*")


def occurrence_id(record):
    return (
        record["occurrence_id"] if "occurrence_id" in record else record["event"]["id"]
    )


def portable_namespace(config):
    value = config.get("source_namespace")
    if not isinstance(value, str) or not re.fullmatch(r"[\w.-]{1,128}", value):
        raise ValueError("Portable findings require a stable source_namespace")
    return value


def identifier(source, value):
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise ValueError("Expected a nonempty source occurrence ID")
    return hashlib.sha256(encode([SCHEMA, source, value]).encode()).hexdigest()


def string_field(row, field, limit, required=False):
    value = row.get(field, "")
    if (
        not isinstance(value, str)
        or len(value) > limit
        or (required and not value.strip())
    ):
        raise ValueError("Invalid finding field: " + field)
    return value


def fingerprint(row):
    data = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode()).hexdigest()


def validate_supersession(retired):
    done = set()
    for origin in retired:
        active = set()
        current = origin
        while current in retired and current not in done:
            if current in active:
                raise ValueError("Portable finding supersession contains a cycle")
            active.add(current)
            current = retired[current]
        done.update(active)


def read_batch(config, batch, state):
    source = portable_namespace(config)
    if (
        not isinstance(batch, dict)
        or batch.get("schema") != SCHEMA
        or batch.get("source") != source
    ):
        raise ValueError("Portable batch schema or source does not match configuration")
    rows = batch.get("findings")
    if not isinstance(rows, list) or len(rows) > config.get("population_limit", 10000):
        raise ValueError("Invalid or oversized portable finding batch")
    pending = state.get("backlog", {}).copy()
    retired = state.get("superseded_occurrences", {}).copy()
    fingerprints = state.get("portable_occurrence_fingerprints", {}).copy()
    current = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Expected a finding object")
        if {"occurrence_id", "source_namespace", "event_ref"}.intersection(row):
            raise ValueError("Portable finding uses a reserved field")
        key = identifier(source, row.get("id"))
        if key in current:
            raise ValueError("Repeated occurrence ID in portable batch")
        string_field(row, "summary", 4000, required=True)
        timestamp(string_field(row, "observed_at", 80, required=True))
        for field in ("impact", "evidence", "recommendation"):
            string_field(row, field, 16000)
        for field in ("group", "unit", "evidence_ref"):
            string_field(row, field, 512)
        url = string_field(row, "issue_url", 512)
        if url and not ISSUE.fullmatch(url):
            raise ValueError("Invalid canonical issue URL")
        for field in ("origins", "supersedes"):
            values = row.get(field, [])
            if (
                not isinstance(values, list)
                or len(values) > 100
                or any(
                    not isinstance(v, str) or not v.strip() or len(v) > 512
                    for v in values
                )
            ):
                raise ValueError("Invalid finding field: " + field)
        for old in row.get("supersedes", []):
            previous = identifier(source, old)
            if previous == key:
                raise ValueError("A finding cannot supersede itself")
            if previous in retired and retired[previous] != key:
                raise ValueError("A finding already has a different successor")
            retired[previous] = key
        digest = fingerprint(row)
        if key not in fingerprints and key in pending:
            fingerprints[key] = fingerprint(
                {
                    k: v
                    for k, v in pending[key].items()
                    if k not in ("occurrence_id", "source_namespace")
                }
            )
        if key in fingerprints and fingerprints[key] != digest:
            raise ValueError(
                "A repeated occurrence ID changed; use a new ID and supersedes"
            )
        fingerprints[key] = digest
        current[key] = row | {"occurrence_id": key, "source_namespace": source}
    validate_supersession(retired)
    pending.update(current)
    for key in list(pending):
        if key in retired or key in state.get("events", {}):
            pending.pop(key)
    state.update(
        backlog=pending,
        superseded_occurrences=retired,
        portable_occurrence_fingerprints=fingerprints,
    )
    return [r for r in pending.values() if r.get("source_namespace") == source]


def prepare_portable(records, existing, previous, retry_ids):
    events, catalog, prepared, groups, membership = {}, [], [], {}, {}
    units, group_units = {}, {}
    for i, row in enumerate(sorted(records, key=occurrence_id), 1):
        ref = f"e{i}"
        key = occurrence_id(row)
        group = row.get("group") or key
        unit = row.get("unit") or key
        unit_ref = units.setdefault(unit, f"u{len(units) + 1}")
        events[ref] = key
        membership[key] = group
        item = {
            "event_ref": ref,
            "group": group,
            "unit_ref": unit_ref,
            "summary": row["summary"],
            "impact": row.get("impact", ""),
            "observed_at": row["observed_at"],
            "possible_origins": row.get("origins", []),
            "issue_url": row.get("issue_url"),
        }
        catalog.append(item)
        prepared.append(row | {"event_ref": ref})
        group_card = groups.setdefault(
            group, {"group_id": group, "events": 0, "distinct_units": 0, "examples": []}
        )
        group_card["events"] += 1
        seen = group_units.setdefault(group, set())
        if unit_ref not in seen:
            seen.add(unit_ref)
            group_card["distinct_units"] += 1
            if len(group_card["examples"]) < 3:
                group_card["examples"].append(item)
    context = {
        "finding_source": "portable",
        "total_events": len(records),
        "automations": {},
        "event_catalog": catalog,
        "groups": list(groups.values()),
        "existing_work": existing,
        "previous_attempts": list(previous),
        "explicit_retry_topics": [t for t in previous if t["id"] in retry_ids],
    }
    return (
        context,
        {"events": events, "event_groups": membership, "existing_work": existing},
        prepared,
    )
