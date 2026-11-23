import datetime as dt
import hashlib
import json
import random
import secrets
import uuid
from collections import Counter
from pathlib import Path

from common import encode, redact, timestamp, write_json
from attribution import automation_ids, candidates as owner_candidates, ownership
from flow1 import estimated_cost, signal_definition
from laminar import META, SESSION, object_value


def collect_events(client, handled, *, aggregate=False):
    args = client.args
    until = timestamp(
        args.until or (dt.datetime.now(dt.UTC) - dt.timedelta(minutes=10)).isoformat()
    )
    since = timestamp(args.since or (until - dt.timedelta(days=7)).isoformat())
    if not dt.timedelta(0) < until - since <= dt.timedelta(days=7):
        raise ValueError("Choose a positive window of at most seven days")
    identifier = str(uuid.UUID(args.signal_id))
    definition = signal_definition(
        client.request(client.base + "/v1/signals/" + identifier), args.project_id
    )
    profile = client.request(
        client.base + "/v1/llm-profiles/" + definition["llmProfileId"]
    )
    profile = {key: profile[key] for key in ("id", "provider", "config", "models")}
    definitions = {str(definition["version"]): definition}
    if aggregate:
        from triage import definition_key

        history = client.request(
            client.base + "/v1/signals/" + identifier + "/versions"
        )
        for entry in history["versions"]:
            fields = dict(entry["definition"])
            if "llmModel" in fields:
                fields["model"] = fields.pop("llmModel")
            if "structuredOutputSchema" in fields:
                fields["structuredOutput"] = fields.pop("structuredOutputSchema")
            older = definition | fields | {"version": entry["version"]}
            if definition_key(older) == definition_key(definition):
                definitions[str(older["version"])] = signal_definition(
                    older, args.project_id
                )
    versions = ",".join(str(int(version)) for version in definitions)
    scope = (
        f"signal_id='{identifier}' AND signal_version IN ({versions}) "
        f"AND updated_at >= '{since.strftime('%Y-%m-%d %H:%M:%S')}' "
        f"AND updated_at < '{until.strftime('%Y-%m-%d %H:%M:%S')}'"
    )
    rows = client.query(
        "SELECT signal_id,signal_version,run_id,trace_id,status,event_id,updated_at,"
        "input_tokens,cache_read_tokens,output_tokens FROM signal_runs "
        f"WHERE {scope} ORDER BY updated_at,run_id LIMIT {args.population_limit + 1}"
    )
    if len(rows) > args.population_limit:
        raise ValueError("Signal run window exceeds its population budget")
    attempts, latest = {}, {}
    for run in sorted(
        rows, key=lambda row: (timestamp(row["updated_at"]), row["run_id"])
    ):
        attempts[run["run_id"]] = run
        latest[run["trace_id"]] = run
    candidates = {
        str(uuid.UUID(run["event_id"])): run
        for run in latest.values()
        if run["status"] == "COMPLETED"
        and run.get("event_id") not in (None, "", str(uuid.UUID(int=0)))
        and run["event_id"] not in handled
    }
    seed = args.seed if args.seed is not None else secrets.randbits(64)
    selected = (
        sorted(candidates)
        if aggregate
        else random.Random(seed).sample(
            sorted(candidates), min(args.limit, len(candidates))
        )
    )
    pricing = json.loads(Path(__file__).with_name("flow1-pricing.json").read_text())
    snapshot = {
        "schema_version": 1,
        "project_id": args.project_id,
        "app_url": args.app_url.rstrip("/"),
        "api_origin": client.base,
        "analysis_backend": "flow1",
        "flow1_signal": definition,
        "flow1_definitions": definitions,
        "flow1_profile": profile,
        "flow1_pricing": pricing,
        "window": {"since": since.isoformat(), "until": until.isoformat()},
        "selection": {
            "method": "all_unhandled_events"
            if aggregate
            else "uniform_random_without_replacement",
            "unit": "unhandled_signal_event",
            "seed": seed,
            "population_size": len(candidates),
            "sample_size": len(selected),
            "selected_event_ids": selected,
            "inclusion_probability": len(selected) / len(candidates)
            if candidates
            else 0,
            "population_sha256": hashlib.sha256(
                encode(sorted(candidates)).encode()
            ).hexdigest(),
            "frame": "Latest completed finding events of compatible Signal versions; upstream trace sampling is separate"
            if aggregate
            else "Latest completed finding events of this Signal version; upstream trace sampling is separate",
        },
        "signal_run_summary": {
            "attempts": len(attempts),
            "statuses": dict(Counter(run["status"] for run in attempts.values())),
            "completed_no_event": sum(
                run["status"] == "COMPLETED"
                and run.get("event_id") in (None, "", str(uuid.UUID(int=0)))
                and int(run.get("input_tokens", 0)) + int(run.get("output_tokens", 0))
                > 0
                for run in attempts.values()
            ),
            "completed_without_recorded_usage": sum(
                run["status"] == "COMPLETED"
                and int(run.get("input_tokens", 0)) + int(run.get("output_tokens", 0))
                == 0
                for run in attempts.values()
            ),
            "estimated_gross_usd": estimated_cost(list(attempts.values()), pricing)
            if attempts
            else 0,
            "estimated_cost_scope": "all observed attempts in the window, not new charges for this reader",
        },
        "samples": [],
        "collection_errors": [],
        "attribution_warnings": [],
    }
    if aggregate:
        snapshot["latest_runs"] = list(latest.values())
        snapshot["events"] = collect_cards(client, snapshot, candidates)
        snapshot["collection_measurements"] = client.measurements
        snapshot = redact(snapshot)
        snapshot["selection"]["population_sha256"] = hashlib.sha256(
            encode(sorted(candidates)).encode()
        ).hexdigest()
        write_json(args.output / "input.json", snapshot)
        return snapshot
    grouped = {}
    for event_id in selected:
        run = candidates[event_id]
        trace_id = str(uuid.UUID(run["trace_id"]))
        owners = client.query(
            f"SELECT DISTINCT JSONExtractString(attributes,'{SESSION}') AS conversation_id,"
            f"JSONExtractString(attributes,'{META}automation.id') AS automation_id,"
            f"JSONExtractString(attributes,'{META}automation.run_id') AS run_id,"
            f"JSONExtractString(attributes,'{META}automation.name') AS automation_name "
            f"FROM spans WHERE trace_id='{trace_id}' AND name='automation.conversation' LIMIT 3"
        )
        if len(owners) != 1 or not owners[0]["conversation_id"]:
            snapshot["collection_errors"].append(
                {
                    "event_id": event_id,
                    "error": "Missing or ambiguous automation/conversation ownership",
                }
            )
            continue
        owner = owners[0]
        conversation_id = str(uuid.UUID(owner["conversation_id"]))
        case = grouped.setdefault(
            conversation_id,
            {**owner, "selected_event_ids": [], "flow1_required_trace_ids": []},
        )
        case["selected_event_ids"].append(event_id)
        case["flow1_required_trace_ids"].append(trace_id)
    for index, owner in enumerate(grouped.values(), 1):
        sample = {**owner, "case_id": f"c{index}", "spans": [], "signals": []}
        snapshot["samples"].append(sample)
        try:
            client.load_sample(sample)
            client.load_flow1(sample, definition)
        except (ValueError, KeyError, OSError) as error:
            sample["collection_error"] = str(error)
    snapshot["collection_measurements"] = client.measurements
    snapshot = redact(snapshot)
    snapshot["selection"]["population_sha256"] = hashlib.sha256(
        encode(sorted(candidates)).encode()
    ).hexdigest()
    write_json(args.output / "input.json", snapshot)
    return snapshot


def collect_cards(client, snapshot, candidates):
    records = []
    identifiers = sorted(candidates)
    for offset in range(0, len(identifiers), 100):
        batch = identifiers[offset : offset + 100]
        traces = sorted({str(uuid.UUID(candidates[key]["trace_id"])) for key in batch})
        trace_ids = ",".join("'" + key + "'" for key in traces)
        event_ids = ",".join("'" + key + "'" for key in batch)
        owners = client.query(
            f"SELECT DISTINCT trace_id,JSONExtractString(attributes,'{SESSION}') AS conversation_id,"
            f"JSONExtractString(attributes,'{META}automation.id') AS automation_id,"
            f"JSONExtractString(attributes,'{META}automation.run_id') AS run_id,"
            f"JSONExtractString(attributes,'{META}automation.name') AS automation_name "
            f"FROM spans WHERE trace_id IN ({trace_ids}) AND name='automation.conversation' LIMIT {len(batch) * 3 + 1}"
        )
        events = client.query(
            "SELECT id,signal_id,signal_version,run_id,trace_id,payload,severity,timestamp,leaf_clusters "
            f"FROM signal_events WHERE signal_id='{snapshot['flow1_signal']['id']}' "
            f"AND id IN ({event_ids}) LIMIT {len(batch) + 1}"
        )
        ownership_incomplete = len(owners) > len(batch) * 3
        if len(events) > len(batch):
            snapshot["collection_errors"].extend(
                {
                    "event_id": key,
                    "error": "Ownership/event lookup exceeded its row budget",
                }
                for key in batch
            )
            continue
        by_id = {}
        for event in events:
            by_id.setdefault(event["id"], []).append(event)
        for event_id in batch:
            run = candidates[event_id]
            matches = [
                owner for owner in owners if owner["trace_id"] == run["trace_id"]
            ]
            try:
                if len(by_id.get(event_id, [])) != 1:
                    raise ValueError("Missing or ambiguous Signal event")
                event = by_id[event_id][0]
                attribution = ownership(matches, ownership_incomplete)
                if any(
                    event[key] != run[key]
                    for key in ("signal_id", "signal_version", "run_id", "trace_id")
                ):
                    raise ValueError("Finding differs from its completed Signal run")
                payload = object_value(event["payload"])
                if not payload or not isinstance(payload.get("summary"), str):
                    raise ValueError("Finding has no structured summary")
                records.append(
                    {
                        "event": event,
                        **attribution,
                        "run": run,
                        "definition": snapshot["flow1_definitions"][
                            str(event["signal_version"])
                        ],
                    }
                )
                if attribution["owner"] is None:
                    snapshot.setdefault("attribution_warnings", []).append(
                        {
                            "event_id": event_id,
                            "status": attribution["attribution_status"],
                            "candidate_count": len(matches),
                        }
                    )
            except (ValueError, KeyError, TypeError) as error:
                snapshot["collection_errors"].append(
                    {"event_id": event_id, "error": str(error)}
                )
    return records


def hydrate_events(client, inventory, records):
    grouped = {}
    for record in records:
        owner, event = record["owner"], record["event"]
        if owner is None:
            grouped["trace:" + event["trace_id"]] = {
                "conversation_id": None,
                "automation_id": next(iter(automation_ids(record)), None)
                if len(automation_ids(record)) == 1
                else None,
                "trace_id": event["trace_id"],
                "owner_candidates": owner_candidates(record),
                "attribution_status": record["attribution_status"],
                "selected_event_ids": [event["id"]],
                "flow1_required_trace_ids": [event["trace_id"]],
                "flow1_signal": record["definition"],
                "flow1_events": [event],
                "flow1_runs": [record["run"]],
            }
            continue
        conversation = owner["conversation_id"]
        if conversation in grouped:
            raise ValueError("Select at most one representative per conversation")
        grouped[conversation] = {
            **owner,
            "selected_event_ids": [event["id"]],
            "flow1_required_trace_ids": [event["trace_id"]],
            "flow1_signal": record["definition"],
        }
    samples = []
    for index, owner in enumerate(grouped.values(), 1):
        sample = {**owner, "case_id": f"c{index}", "spans": [], "signals": []}
        samples.append(sample)
        try:
            if sample.get("attribution_status"):
                trace = str(uuid.UUID(sample["trace_id"]))
                sample["trace_ids"] = [trace]
                sample["signals"] = sample["flow1_events"]
                sample["attribution_warning"] = (
                    "Trace-level finding; candidate conversations are not confirmed negative inputs. Resolve attribution from cited spans before replay."
                )
                sample["span_index"] = client.query(
                    f"SELECT span_id,parent_span_id,name,span_type,status,start_time,end_time,JSONExtractString(attributes,'{SESSION}') AS conversation_id,length(input) AS input_bytes,length(output) AS output_bytes FROM spans WHERE trace_id='{trace}' ORDER BY start_time,span_id LIMIT {client.args.max_spans + 1}"
                )
                sample["span_index_clipped"] = (
                    len(sample["span_index"]) > client.args.max_spans
                )
                sample["span_index"] = sample["span_index"][: client.args.max_spans]
                continue
            client.load_sample(sample)
            client.load_flow1(sample, sample["flow1_signal"])
        except (ValueError, KeyError, OSError) as error:
            sample["collection_error"] = str(error)
    return {
        key: value
        for key, value in inventory.items()
        if key not in ("events", "latest_runs")
    } | {
        "samples": samples,
        "selection": inventory["selection"]
        | {
            "method": "luna_ranked_representatives_per_topic",
            "selected_event_ids": [record["event"]["id"] for record in records],
            "sample_size": len(records),
            "inclusion_probability": None,
        },
    }
