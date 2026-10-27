import datetime as dt
import hashlib
import json
import os
import random
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from common import encode, redact, timestamp, write_json

SESSION = "lmnr.association.properties.session_id"
META = "lmnr.association.properties.metadata."


def object_value(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {}
    return value if isinstance(value, dict) else {}


def message_projection(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return value
    if isinstance(value, list):
        return [message_projection(item) for item in value]
    if isinstance(value, dict) and "role" in value:
        return {
            key: value[key]
            for key in (
                "role",
                "content",
                "tool_calls",
                "tool_call_id",
                "function_call",
                "name",
            )
            if value.get(key) is not None
        }
    return value


class Laminar:
    def __init__(self, args):
        self.args = args
        self.key = os.environ.get("LMNR_PROJECT_API_KEY", "")
        self.base = os.environ.get("LMNR_BASE_URL", "").rstrip("/")
        parsed = urllib.parse.urlparse(self.base)
        if not self.key or parsed.scheme not in ("https", "http"):
            raise ValueError("Set LMNR_PROJECT_API_KEY and an explicit LMNR_BASE_URL")
        if parsed.scheme == "http" and parsed.hostname not in (
            "localhost",
            "127.0.0.1",
            "::1",
        ):
            raise ValueError("Remote Laminar requires HTTPS")
        self.measurements = []

    def request(self, url, body=None, timeout=30, method=None):
        request = urllib.request.Request(
            url,
            data=encode(body).encode() if body is not None else None,
            method=method,
            headers={
                "Authorization": "Bearer " + self.key,
                "Content-Type": "application/json",
            },
        )
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read(self.args.max_response_bytes + 1)
        except urllib.error.HTTPError as error:
            raise ValueError(f"Laminar HTTP {error.code}; no automatic retry") from None
        if len(raw) > self.args.max_response_bytes:
            raise ValueError("Laminar response exceeds its byte budget")
        self.measurements.append(
            {"seconds": round(time.monotonic() - started, 3), "bytes": len(raw)}
        )
        return json.loads(raw)

    def query(self, sql):
        return self.request(self.base + "/v1/sql/query", {"query": sql})["data"]

    def collect(self):
        until = timestamp(
            self.args.until
            or (dt.datetime.now(dt.UTC) - dt.timedelta(minutes=10)).isoformat()
        )
        since = timestamp(self.args.since or (until - dt.timedelta(days=7)).isoformat())
        if not dt.timedelta(0) < until - since <= dt.timedelta(days=7):
            raise ValueError("Choose a positive window of at most seven days")
        window = f"start_time >= '{since.strftime('%Y-%m-%d %H:%M:%S')}' AND start_time < '{until.strftime('%Y-%m-%d %H:%M:%S')}'"
        definition = None
        profile = None
        signal_scope = ""
        if getattr(self.args, "analyzer", None) == "flow1":
            from flow1 import signal_definition

            identifier = str(uuid.UUID(self.args.signal_id))
            definition = signal_definition(
                self.request(self.base + "/v1/signals/" + identifier),
                self.args.project_id,
            )
            profile = self.request(
                self.base + "/v1/llm-profiles/" + definition["llmProfileId"]
            )
            profile = {
                key: profile[key] for key in ("id", "provider", "config", "models")
            }
            if (
                profile["provider"] != "custom"
                or profile["config"].get("baseUrl", "").rstrip("/")
                != "https://api.lmnr.ai/v1"
            ):
                raise ValueError(
                    "Flow-1 requires the Laminar Cloud Chat Completions profile"
                )
            signal_scope = (
                f" AND trace_id IN (SELECT trace_id FROM signal_runs WHERE signal_id='{identifier}' "
                f"AND signal_version={definition['version']})"
            )
        population = self.query(
            f"SELECT JSONExtractString(attributes, '{SESSION}') AS conversation_id, "
            "groupUniqArray(trace_id) AS trace_ids, "
            f"any(JSONExtractString(attributes, '{META}automation.id')) AS automation_id, "
            f"any(JSONExtractString(attributes, '{META}automation.run_id')) AS run_id, "
            f"any(JSONExtractString(attributes, '{META}automation.name')) AS automation_name, "
            "min(start_time) AS started_at FROM spans "
            f"WHERE {window} AND name = 'automation.conversation'{signal_scope} "
            "GROUP BY conversation_id HAVING conversation_id != '' "
            f"ORDER BY conversation_id LIMIT {self.args.population_limit + 1}"
        )
        if not population or len(population) > self.args.population_limit:
            raise ValueError(
                "Empty or oversized sampling frame; change the window or population limit"
            )
        population.sort(key=lambda item: item["conversation_id"])
        seed = self.args.seed if self.args.seed is not None else secrets.randbits(64)
        selected = random.Random(seed).sample(
            population, min(self.args.limit, len(population))
        )
        snapshot = {
            "schema_version": 1,
            "project_id": self.args.project_id,
            "app_url": self.args.app_url.rstrip("/"),
            "api_origin": self.base,
            "window": {"since": since.isoformat(), "until": until.isoformat()},
            "selection": {
                "method": "uniform_random_without_replacement",
                "unit": "unique_conversation",
                "seed": seed,
                "population_size": len(population),
                "sample_size": len(selected),
                "inclusion_probability": len(selected) / len(population),
                "population_sha256": hashlib.sha256(
                    encode([item["conversation_id"] for item in population]).encode()
                ).hexdigest(),
                "selected_conversation_ids": [
                    item["conversation_id"] for item in selected
                ],
                "frame": "Instrumented automation.conversation spans starting in the window; project-key scoped, not every automation run",
            },
            "samples": [],
        }
        if definition:
            snapshot["analysis_backend"] = "flow1"
            snapshot["flow1_signal"] = definition
            snapshot["flow1_profile"] = profile
            snapshot["flow1_pricing"] = json.loads(
                Path(__file__).with_name("flow1-pricing.json").read_text()
            )
            snapshot["selection"]["frame"] += (
                "; restricted to conversations with a run of this Signal version, "
                "including pending/failed runs, without weighting by findings"
            )
        for index, candidate in enumerate(selected, 1):
            sample = {**candidate, "case_id": f"c{index}", "spans": [], "signals": []}
            snapshot["samples"].append(sample)
            try:
                self.load_sample(sample)
                if definition:
                    self.load_flow1(sample, definition)
            except (ValueError, KeyError, OSError) as error:
                sample["collection_error"] = str(error)
        snapshot["collection_measurements"] = self.measurements
        snapshot["redaction"] = (
            "Best effort; private replay artifacts require review before sharing"
        )
        population_hash = snapshot["selection"]["population_sha256"]
        snapshot = redact(snapshot)
        snapshot["selection"]["population_sha256"] = population_hash
        write_json(self.args.output / "input.json", snapshot)
        return snapshot

    def load_sample(self, sample):
        session = str(uuid.UUID(sample["conversation_id"]))
        trace_rows = self.query(
            f"SELECT DISTINCT trace_id FROM spans WHERE JSONExtractString(attributes, '{SESSION}') = '{session}' "
            f"ORDER BY trace_id LIMIT {self.args.max_spans + 1}"
        )
        if not trace_rows or len(trace_rows) > self.args.max_spans:
            raise ValueError("Missing or oversized conversation trace scope")
        ids = ",".join(
            "'" + str(uuid.UUID(row["trace_id"])) + "'" for row in trace_rows
        )
        condition = (
            f"trace_id IN ({ids}) AND (JSONExtractString(attributes, '{SESSION}') = '{session}' "
            f"OR JSONExtractString(attributes, '{SESSION}') = '')"
        )
        columns = (
            "span_id,parent_span_id,trace_id,name,span_type,status,start_time,end_time,"
            "substring(input,1,4000) AS input_excerpt,if(span_type='LLM',output,substring(output,1,6000)) AS output_excerpt,"
            "length(input) AS original_input_bytes,length(output) AS original_output_bytes,"
            "model,total_tokens,total_cost"
        )
        rows = self.query(
            f"SELECT {columns} FROM spans WHERE {condition} "
            f"ORDER BY start_time,span_id LIMIT {self.args.max_spans + 1}"
        )
        if not rows or len(rows) > self.args.max_spans:
            raise ValueError(
                "Selected conversation has no spans or exceeds --max-spans; not resampled"
            )
        sample["trace_ids"] = sorted({row["trace_id"] for row in rows})
        for index, row in enumerate(rows, 1):
            llm = row["span_type"] == "LLM"
            sample["spans"].append(
                {
                    **{
                        key: value
                        for key, value in row.items()
                        if key not in ("input_excerpt", "output_excerpt")
                    },
                    "ref": f"s{index}",
                    "input": None if llm else row["input_excerpt"],
                    "output": message_projection(row["output_excerpt"])
                    if llm
                    else row["output_excerpt"],
                    "llm_input_omitted": llm,
                    "content_clipped": (not llm and row["original_input_bytes"] > 4000)
                    or (not llm and row["original_output_bytes"] > 6000),
                    "output_projection": "assistant behavior; reasoning/provider metadata omitted"
                    if llm
                    else "tool/default excerpt",
                }
            )
        initial = self.query(
            f"SELECT input FROM spans WHERE {condition} AND name='conversation.send_message' ORDER BY start_time,span_id LIMIT 21"
        )
        if len(initial) > 20:
            raise ValueError("More than 20 task messages; not resampled")
        messages = [object_value(row["input"]).get("message") for row in initial]
        if not messages:
            initial = self.query(
                f"SELECT input FROM spans WHERE {condition} AND span_type='LLM' AND input != '' ORDER BY start_time,span_id LIMIT 1"
            )
            value = object_value(initial[0]["input"]) if initial else {}
            messages = value.get("messages", value.get("input", []))
        if isinstance(messages, list):
            sample["initial_instructions"] = [
                message_projection(message)
                for message in messages
                if isinstance(message, dict)
                and message.get("role") in ("system", "developer", "user")
            ]
        else:
            sample["initial_instructions"] = []
        sample["instruction_coverage"] = (
            "SDK send_message inputs, falling back to first nonempty LLM input; accumulated LLM histories omitted"
        )
        if len(encode(sample).encode()) > self.args.max_sample_bytes:
            raise ValueError(
                "Selected conversation exceeds --max-sample-bytes; not resampled"
            )
        if getattr(self.args, "analyzer", None) == "flow1":
            return
        ids = ",".join(
            "'" + str(uuid.UUID(value)) + "'" for value in sample["trace_ids"]
        )
        try:
            signals = self.query(
                f"SELECT id,trace_id,name,payload,timestamp FROM signal_events WHERE trace_id IN ({ids}) ORDER BY timestamp LIMIT 51"
            )
            sample["signals"] = signals[:50]
            sample["signal_coverage"] = (
                "first_50" if len(signals) > 50 else "complete_at_collection"
            )
        except (ValueError, OSError):
            sample["signal_coverage"] = "unavailable"

    def load_flow1(self, sample, definition):
        ownership_ids = ",".join(
            "'" + str(uuid.UUID(value)) + "'" for value in sample["trace_ids"]
        )
        sample["flow1_trace_sessions"] = self.query(
            f"SELECT trace_id,groupUniqArray(JSONExtractString(attributes, '{SESSION}')) AS conversation_ids "
            f"FROM spans WHERE trace_id IN ({ownership_ids}) AND JSONExtractString(attributes, '{SESSION}') != '' GROUP BY trace_id"
        )
        ids = ",".join(
            "'" + str(uuid.UUID(value)) + "'"
            for value in sample.get("flow1_required_trace_ids", sample["trace_ids"])
        )
        scope = (
            f"signal_id='{definition['id']}' AND signal_version={definition['version']} "
            f"AND trace_id IN ({ids})"
        )
        runs = self.query(
            "SELECT signal_id,signal_version,run_id,trace_id,status,error_message,event_id,"
            "updated_at,input_tokens,cache_read_tokens,output_tokens "
            f"FROM signal_runs WHERE {scope} ORDER BY updated_at,run_id LIMIT 51"
        )
        if len(runs) > 50:
            raise ValueError("Flow-1 run count exceeds its budget")
        events = self.query(
            "SELECT id,signal_id,signal_version,run_id,trace_id,name,payload,timestamp,severity "
            f"FROM signal_events WHERE {scope} ORDER BY timestamp,id LIMIT 51"
        )
        if len(events) > 50:
            raise ValueError("Flow-1 event count exceeds its budget")
        sample["flow1_runs"] = runs
        sample["flow1_events"] = events
        sample["signals"] = events
        sample["signal_coverage"] = "selected_flow1_signal_version"
        if len(encode(sample).encode()) > self.args.max_sample_bytes:
            raise ValueError("Flow-1 evidence exceeds --max-sample-bytes")
