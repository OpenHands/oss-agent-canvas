import re
import urllib.parse
import uuid

from common import timestamp
from laminar import object_value


def signal_definition(value, project_id):
    if (
        value.get("projectId") != project_id
        or value.get("model") != "flow-1"
        or not value.get("llmProfileId")
        or not isinstance(value.get("version"), int)
        or value["version"] < 1
    ):
        raise ValueError("Require a versioned flow-1 Signal in the selected project")
    uuid.UUID(value["id"])
    uuid.UUID(value["llmProfileId"])
    return value


def estimated_cost(runs, pricing):
    if not pricing:
        return None
    latest = {
        run["run_id"]: run
        for run in sorted(runs, key=lambda run: timestamp(run["updated_at"]))
    }
    total = 0.0
    for run in latest.values():
        input_tokens = int(run.get("input_tokens", 0))
        cached_tokens = int(run.get("cache_read_tokens", 0))
        output_tokens = int(run.get("output_tokens", 0))
        if (
            min(input_tokens, cached_tokens, output_tokens) < 0
            or cached_tokens > input_tokens
            or input_tokens + output_tokens == 0
        ):
            return None
        total += (
            (input_tokens - cached_tokens) * pricing["input_usd_per_million"]
            + cached_tokens * pricing["cached_input_usd_per_million"]
            + output_tokens * pricing["output_usd_per_million"]
        ) / 1_000_000
    return total


def findings(snapshot, sample):
    definition = signal_definition(
        sample.get("flow1_signal", snapshot["flow1_signal"]), snapshot["project_id"]
    )
    if definition["id"] != snapshot["flow1_signal"]["id"]:
        raise ValueError("Representative definition belongs to another Signal")
    profile = snapshot["flow1_profile"]
    if (
        profile["id"] != definition["llmProfileId"]
        or profile["provider"] != "custom"
        or profile["config"].get("baseUrl", "").rstrip("/") != "https://api.lmnr.ai/v1"
        or "flow-1" not in profile["models"]
    ):
        raise ValueError("Frozen profile is not the Laminar Cloud flow-1 route")
    ownership = {
        row["trace_id"]: set(row["conversation_ids"])
        for row in sample.get("flow1_trace_sessions", [])
    }
    if set(ownership) != set(sample["trace_ids"]) or any(
        sessions != {sample["conversation_id"]} for sessions in ownership.values()
    ):
        raise ValueError(
            "Flow-1 trace is shared across conversations or ownership is missing"
        )
    if not sample.get("initial_instructions") or not any(
        (span.get("input") or span.get("output"))
        and span["name"] not in ("conversation.send_message", "conversation.run")
        for span in sample["spans"]
    ):
        raise ValueError(
            "Insufficient task/execution content; lifecycle spans are not a trajectory"
        )
    all_traces = set(sample["trace_ids"])
    expected = set(sample.get("flow1_required_trace_ids", sample["trace_ids"]))
    if not expected or not expected <= all_traces:
        raise ValueError("Invalid selected Flow-1 trace scope")
    if all_traces != {span["trace_id"] for span in sample["spans"]}:
        raise ValueError("Conversation trace scope differs from frozen spans")
    runs = sample.get("flow1_runs", [])
    latest = {}
    for run in runs:
        if (
            run["signal_id"] != definition["id"]
            or run["trace_id"] not in expected
            or run["signal_version"] != definition["version"]
        ):
            raise ValueError("Flow-1 run scope/version differs from frozen evidence")
        previous = latest.get(run["trace_id"])
        if previous is None or timestamp(run["updated_at"]) > timestamp(
            previous["updated_at"]
        ):
            latest[run["trace_id"]] = run
    if set(latest) != expected or any(
        run["status"] != "COMPLETED" for run in latest.values()
    ):
        raise ValueError("Missing, pending or failed Flow-1 analysis; not a clean case")
    if any(
        sum(int(run.get(field, 0)) for field in ("input_tokens", "output_tokens")) == 0
        for run in latest.values()
    ):
        raise ValueError(
            "Flow-1 run has no recorded model usage; skipped analysis is not clean"
        )
    late_lifecycle = []
    for span in sample["spans"]:
        if (
            span["trace_id"] in expected
            and span.get("end_time")
            and timestamp(span["end_time"])
            > timestamp(latest[span["trace_id"]]["updated_at"])
        ):
            if (
                span["name"] == "conversation"
                and span["span_type"] == "DEFAULT"
                and not span.get("input")
                and not span.get("output")
            ):
                late_lifecycle.append(span["ref"])
                continue
            raise ValueError(
                "Trace changed after Flow-1 analysis; refresh its Signal run"
            )
    by_run = {run["run_id"]: run for run in latest.values()}
    events = sample.get("flow1_events", [])
    result, event_ids, finding_event_ids = [], set(), []
    selected_events = set(sample.get("selected_event_ids", []))
    for event in events:
        if event["run_id"] not in by_run:
            continue
        run = by_run[event["run_id"]]
        if (
            event["signal_id"] != definition["id"]
            or event["signal_version"] != definition["version"]
            or event["trace_id"] != run["trace_id"]
        ):
            raise ValueError("Flow-1 event scope/version mismatch")
        if event["id"] in event_ids:
            raise ValueError("Duplicate Flow-1 event")
        event_ids.add(event["id"])
        if selected_events and event["id"] not in selected_events:
            continue
        finding = object_value(event["payload"])
        evidence = finding.get("evidence")
        if not isinstance(evidence, str):
            raise ValueError("Flow-1 evidence must contain trace/span links")
        refs = []
        links = re.findall(r"https?://[^\s)<>]+", evidence)
        for link in links:
            url = urllib.parse.urlparse(link)
            parts = url.path.strip("/").split("/")
            query = urllib.parse.parse_qs(url.query)
            if (
                len(parts) != 4
                or parts[0] != "project"
                or parts[1] != snapshot["project_id"]
                or parts[2] != "traces"
                or parts[3] != event["trace_id"]
                or len(query.get("spanId", [])) != 1
            ):
                raise ValueError("Flow-1 citation points outside its project/trace")
            identifier = str(uuid.UUID(query["spanId"][0]))
            matches = []
            for span in sample["spans"]:
                original = uuid.UUID(span["span_id"])
                aliases = {str(original)}
                if span["span_type"] == "LLM":
                    aliases.add(str(uuid.UUID(int=(original.int + 1) % (1 << 128))))
                if span["trace_id"] == event["trace_id"] and identifier in aliases:
                    matches.append(span["ref"])
            if len(matches) != 1:
                raise ValueError("Unknown or ambiguous Flow-1 span citation")
            if matches[0] not in refs:
                refs.append(matches[0])
        if not refs:
            raise ValueError("Flow-1 finding has no valid span citations")
        result.append({**finding, "evidence": refs})
        finding_event_ids.append(event["id"])
    for run in latest.values():
        if (
            run.get("event_id") not in (None, "", str(uuid.UUID(int=0)))
            and run["event_id"] not in event_ids
        ):
            raise ValueError("Completed Flow-1 run references an unavailable finding")
    if selected_events and set(finding_event_ids) != selected_events:
        raise ValueError("Selected Flow-1 finding was superseded or is unavailable")
    return {"findings": result, "finding_event_ids": finding_event_ids}, {
        "backend": "persisted_flow1_signals",
        "configured_model": definition["model"],
        "effective_model": None,
        "signal_id": definition["id"],
        "signal_version": definition["version"],
        "llm_profile_id": definition["llmProfileId"],
        "runs": list(latest.values()),
        "event_ids": sorted(event_ids),
        "late_lifecycle_span_refs": late_lifecycle,
        "reported_cost_usd": None,
        "estimated_cost_usd": estimated_cost(runs, snapshot.get("flow1_pricing")),
        "pricing": snapshot.get("flow1_pricing"),
        "estimated_cost_scope": "all frozen runs of this Signal version, before credits; not a billing receipt",
        "seconds": None,
        "coverage": (
            "selected_finding_traces; other conversation traces are context only"
            if "flow1_required_trace_ids" in sample
            else "all_collected_traces"
        )
        + "; model judgment is not task-quality proof",
    }
