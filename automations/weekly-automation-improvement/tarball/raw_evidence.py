import json
import uuid

from common import encode, redact, write_json
from attribution import conversation_ids, ownership
from laminar import META, SESSION


def schema(name, description, properties, required):
    return {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


UUID = {"type": "string", "format": "uuid"}
PAGE = {"type": "integer", "minimum": 0}


def population(client, inventory, automation_id):
    automation_ids = {
        str(uuid.UUID(value))
        for value in (
            [automation_id] if isinstance(automation_id, str) else automation_id
        )
    }
    if not automation_ids:
        raise ValueError("Select at least one automation")
    latest = inventory.get("latest_runs", [])
    owners = {}
    traces = sorted({str(uuid.UUID(row["trace_id"])) for row in latest})
    for start in range(0, len(traces), 100):
        ids = ",".join(
            "'" + identifier + "'" for identifier in traces[start : start + 100]
        )
        rows = client.query(
            f"SELECT DISTINCT trace_id,JSONExtractString(attributes,'{META}automation.id') AS automation_id,"
            f"JSONExtractString(attributes,'{SESSION}') AS conversation_id,"
            f"JSONExtractString(attributes,'{META}automation.run_id') AS run_id,"
            f"JSONExtractString(attributes,'{META}automation.name') AS automation_name "
            f"FROM spans WHERE trace_id IN ({ids}) AND name='automation.conversation'"
        )
        for row in rows:
            owners.setdefault(row["trace_id"], {})[encode(row)] = row
    result, ambiguous_conversations = [], set()
    for run in latest:
        attribution = ownership(list(owners.get(run["trace_id"], {}).values()))
        if attribution["owner"] is None:
            ambiguous_conversations.update(conversation_ids(attribution))
            continue
        owner = attribution["owner"]
        if owner["automation_id"] not in automation_ids:
            continue
        owner["conversation_id"] = str(uuid.UUID(owner["conversation_id"]))
        event = run.get("event_id")
        has_event = event not in (None, "", str(uuid.UUID(int=0)))
        classified = "unknown"
        if run["status"] == "COMPLETED":
            if has_event:
                classified = "negative"
            elif int(run.get("input_tokens", 0)) + int(run.get("output_tokens", 0)) > 0:
                classified = "non_negative"
        result.append(
            {
                **owner,
                "trace_id": run["trace_id"],
                "signal_event_id": event if has_event else None,
                "classification": classified,
                "signal_run": run,
            }
        )
    uncertain = ambiguous_conversations | {
        row["conversation_id"]
        for row in result
        if row["classification"] != "non_negative"
    }
    return sorted(
        [
            row
            for row in result
            if row["classification"] != "non_negative"
            or row["conversation_id"] not in uncertain
        ],
        key=lambda row: (row["conversation_id"], row["trace_id"]),
    )


class EvidenceReader:
    schemas = [
        schema(
            "search_runs",
            "Find previous negative or non-negative Signal runs for this automation",
            {
                "classification": {
                    "type": "string",
                    "enum": ["negative", "non_negative", "all"],
                },
                "offset": PAGE,
            },
            ["classification", "offset"],
        ),
        schema(
            "read_trace",
            "Read full trace fields in pages; request the next offset to inspect omitted data",
            {
                "trace_id": UUID,
                "span_id": UUID,
                "field": {"type": "string", "enum": ["input", "output"]},
                "offset": PAGE,
            },
            ["trace_id", "span_id", "field", "offset"],
        ),
        schema(
            "list_spans",
            "List span identities and sizes for a trace before reading raw fields",
            {"trace_id": UUID, "offset": PAGE},
            ["trace_id", "offset"],
        ),
        schema(
            "read_signal",
            "Read the original Signal payload rather than its compact card",
            {"event_id": UUID, "offset": PAGE},
            ["event_id", "offset"],
        ),
    ]

    def __init__(self, client, automation_id, inventory, output, *, finding_id=None):
        self.client = client
        self.finding = None
        if finding_id is not None:
            if automation_id is not None:
                raise ValueError("Choose either automation scope or finding scope")
            identifier = str(uuid.UUID(finding_id))
            matching = [
                r for r in inventory.get("events", []) if r["event"]["id"] == identifier
            ]
            if len(matching) != 1:
                raise ValueError("Finding is outside the prepared inventory")
            self.finding = matching[0]
        elif automation_id is None:
            raise ValueError("Choose an automation or prepared finding")
        self.automation_id = (
            str(uuid.UUID(automation_id)) if automation_id is not None else None
        )
        self.inventory = inventory
        self.output = output
        self.calls = 0

    def owner(self, trace_id):
        trace = str(uuid.UUID(trace_id))
        if self.finding is not None:
            if trace != self.finding["event"]["trace_id"]:
                raise ValueError("Trace is outside this finding")
            return trace
        owners = self.client.query(
            f"SELECT DISTINCT JSONExtractString(attributes,'{META}automation.id') AS automation_id FROM spans WHERE trace_id='{trace}' AND JSONExtractString(attributes,'{META}automation.id') != ''"
        )
        if {row["automation_id"] for row in owners} != {self.automation_id}:
            raise ValueError("Trace is outside this automation")
        return trace

    def execute(self, name, arguments):
        specification = next(
            (item for item in self.schemas if item["name"] == name), None
        )
        if specification is None or set(arguments) != set(
            specification["parameters"]["required"]
        ):
            raise ValueError("Unsupported evidence request")
        offset = arguments["offset"]
        if type(offset) is not int or not 0 <= offset <= 10_000_000:
            raise ValueError("Invalid evidence offset")
        self.calls += 1
        if name == "search_runs":
            if arguments["classification"] not in ("negative", "non_negative", "all"):
                raise ValueError("Invalid run classification")
            if self.finding is not None:
                rows = [
                    {
                        "trace_id": self.finding["event"]["trace_id"],
                        "signal_event_id": self.finding["event"]["id"],
                        "classification": "negative",
                        "attribution_status": self.finding.get(
                            "attribution_status", "resolved"
                        ),
                        **{
                            k: v
                            for k, v in self.finding.items()
                            if k in ("owner", "owner_candidates")
                        },
                    }
                ]
            else:
                rows = population(self.client, self.inventory, self.automation_id)
            rows = [
                row
                for row in rows
                if arguments["classification"] == "all"
                or row["classification"] == arguments["classification"]
            ]
            result = {
                "runs": rows[offset : offset + 20],
                "total": len(rows),
                "next_offset": offset + 20 if len(rows) > offset + 20 else None,
            }
        elif name == "read_signal":
            identifier = str(uuid.UUID(arguments["event_id"]))
            if self.finding is not None and identifier != self.finding["event"]["id"]:
                raise ValueError("Signal is outside this finding")
            rows = self.client.query(
                f"SELECT id,trace_id,signal_id,signal_version,severity,payload FROM signal_events WHERE id='{identifier}' LIMIT 2"
            )
            if len(rows) != 1:
                raise ValueError("Signal is missing or ambiguous")
            self.owner(rows[0]["trace_id"])
            if self.finding is not None and any(
                rows[0][key] != self.finding["event"][key]
                for key in ("signal_id", "signal_version")
            ):
                raise ValueError("Signal differs from the prepared finding")
            payload = rows[0].pop("payload")
            text = payload if isinstance(payload, str) else json.dumps(payload)
            result = {
                **rows[0],
                "payload": text[offset : offset + 12000],
                "total_chars": len(text),
                "next_offset": offset + 12000 if len(text) > offset + 12000 else None,
            }
        else:
            trace = self.owner(arguments["trace_id"])
            if name == "list_spans":
                rows = self.client.query(
                    f"SELECT span_id,name,span_type,status,start_time,JSONExtractString(attributes,'{SESSION}') AS conversation_id,length(input) AS input_bytes,length(output) AS output_bytes FROM spans WHERE trace_id='{trace}' ORDER BY start_time,span_id LIMIT 21 OFFSET {offset}"
                )
                result = {
                    "spans": rows[:20],
                    "next_offset": offset + 20 if len(rows) > 20 else None,
                }
            else:
                span = str(uuid.UUID(arguments["span_id"]))
                field = arguments["field"]
                if field not in ("input", "output"):
                    raise ValueError("Invalid trace field")
                rows = self.client.query(
                    f"SELECT span_id,lengthUTF8({field}) AS total_chars,substringUTF8({field},{offset + 1},12000) AS content FROM spans WHERE trace_id='{trace}' AND span_id='{span}' LIMIT 2"
                )
                if len(rows) != 1:
                    raise ValueError("Span is missing or ambiguous")
                result = rows[0] | {
                    "next_offset": offset + 12000
                    if rows[0]["total_chars"] > offset + 12000
                    else None
                }
        result = redact(result)
        write_json(
            self.output / f"read-{self.calls:03d}.json",
            {"tool": name, "arguments": arguments, "result": result},
        )
        return result
