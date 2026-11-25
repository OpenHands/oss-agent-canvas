import argparse
import json
from collections import Counter
from pathlib import Path
from uuid import UUID

from common import read_json
from events import collect_events
from laminar import Laminar
from triage_workflow import arguments
from workflow_runtime import load_environment


def collect(config, output):
    if config.get("finding_source", "laminar") != "laminar":
        raise ValueError("Signal collection requires a Laminar finding source")
    output = output.expanduser().resolve()
    args = arguments(config, output, limit=3)
    load_environment(config, use_model=False)
    output.mkdir(parents=True, mode=0o700, exist_ok=True)
    if any(output.iterdir()):
        raise FileExistsError("Choose a new or empty output directory")
    inventory = collect_events(Laminar(args), set(), aggregate=True)
    latest = inventory["latest_runs"]
    without_event = [
        row
        for row in latest
        if row["status"] == "COMPLETED"
        and row.get("event_id") in (None, "", str(UUID(int=0)))
    ]
    selection = inventory["selection"]
    filters = inventory["trace_filter"]
    basis = filters.get("time_basis", "signal_updated")
    records = inventory["events"]
    return {
        "status": "collection_incomplete"
        if inventory["collection_errors"]
        else "collected",
        "project_id": args.project_id,
        "signal_id": args.signal_id,
        "window": inventory["window"]
        | {
            "time_basis": basis,
            "field": "traces.start_time"
            if basis == "trace_started"
            else "signal_runs.updated_at",
            "bounds": "[since, until)",
        },
        "filter_value_counts": {
            field: len(values)
            for field, values in filters.items()
            if field != "time_basis"
        },
        "signal_attempts": inventory["signal_run_summary"]["attempts"],
        "latest_by_trace": {
            "traces": len(latest),
            "statuses": dict(Counter(row["status"] for row in latest)),
            "completed_without_event": len(without_event),
            "completed_without_event_or_recorded_usage": sum(
                not (
                    int(row.get("input_tokens", 0)) or int(row.get("output_tokens", 0))
                )
                for row in without_event
            ),
        },
        "candidate_events": selection["candidate_event_count"],
        "excluded_by_payload": selection["payload_excluded_count"],
        "matched_findings": len(records),
        "attribution": dict(Counter(row["attribution_status"] for row in records)),
        "collection_errors": len(inventory["collection_errors"]),
        "inventory_path": str(output / "input.json"),
    }


def main():
    parser = argparse.ArgumentParser(
        description="Collect existing Laminar Signal findings"
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "--output", type=Path, required=True, help="New or empty directory"
    )
    parser.add_argument("--since", help="Inclusive window start (ISO 8601)")
    parser.add_argument("--until", help="Exclusive window end (ISO 8601)")
    options = parser.parse_args()
    try:
        config = read_json(options.config)
        if not isinstance(config, dict):
            raise ValueError("The configuration must be a JSON object")
        for field in ("since", "until"):
            if (value := getattr(options, field)) is not None:
                config[field] = value
        summary = collect(config, options.output)
    except KeyError as error:
        parser.exit(1, f"Missing required field: {error.args[0]}\n")
    except (ValueError, OSError) as error:
        parser.exit(1, f"{error}\n")
    print(json.dumps(summary, indent=2))
    return int(summary["status"] == "collection_incomplete")


if __name__ == "__main__":
    raise SystemExit(main())
