import hashlib
import json
import math
import os
import re
import selectors
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from common import redact
from finding_record import occurrence_id
from laminar import object_value
from repair_policy import permits_work
from tracked_work import WORK_URL

INPUT_SCHEMA = "openhands.topic-finder-input.v1"
PLAN_SCHEMA = "openhands.topic-plan.v1"
MAX_OUTPUT_BYTES = 8_000_000
PRIVATE_LOCATION = re.compile(
    r"https?://\S+|(?<![\w/])(?:[A-Za-z]:[\\/]|~/|/)[^\s)\]>`]+"
)


def validate_command(value):
    if (
        not isinstance(value, list)
        or not value
        or any(
            not isinstance(arg, str) or not arg.strip() or "\0" in arg for arg in value
        )
        or not Path(value[0]).is_absolute()
    ):
        raise ValueError(
            "topic_finder_command requires an argv list with an absolute executable"
        )


def text_field(value, bound):
    if not isinstance(value, str):
        return ""
    return PRIVATE_LOCATION.sub("[private location]", redact(value))[:bound]


def build_request(context, records, limit):
    by_ref = {row["event_ref"]: row for row in records}
    findings, identities = [], []
    for card in context["event_catalog"]:
        ref = card["event_ref"]
        row = by_ref[ref]
        identities.append([ref, occurrence_id(row)])
        if "occurrence_id" in row:
            summary, impact = card["summary"], card.get("impact")
            group = row.get("group")
            units = ["unit:" + card["unit_ref"]]
            observed = card["observed_at"]
        else:
            payload = object_value(row["event"]["payload"])
            summary, impact = payload.get("summary"), payload.get("outcome")
            group = card["group"]
            units = ["trace:" + card["trace_ref"]]
            units.extend("conversation:" + alias for alias in card["conversation_refs"])
            observed = row["run"].get("updated_at")
        group_hint = text_field(group, 512)
        if group and group_hint != group:
            group_hint = "group-" + hashlib.sha256(group.encode()).hexdigest()[:20]
        item = {
            "ref": ref,
            "summary": text_field(summary, 4000),
            "group": group_hint or None,
            "unit_refs": units,
        }
        for name, value, bound in (
            ("observed_at", observed, 80),
            ("impact", impact, 4000),
        ):
            if value := text_field(value, bound):
                item[name] = value
        findings.append(item)
    existing = []
    policy = context.get("repair_policy")
    for work in context["existing_work"]:
        match = WORK_URL.fullmatch(work["url"])
        if (
            match
            and not match[4]
            and (not policy or permits_work(policy, match[1], match[2]))
        ):
            existing.append(
                {"url": work["url"], "title": text_field(work.get("title"), 200)}
            )
    guides = [
        {"path": guide["path"], "text": text_field(guide["text"], 2_000_000)}
        for guide in context.get("issue_guides", [])
        if guide["path"] in context.get("issue_guide_files", [])
        and not Path(guide["path"]).is_absolute()
        and ".." not in Path(guide["path"]).parts
    ]
    request = {
        "schema": INPUT_SCHEMA,
        "representative_limit": limit,
        "findings": findings,
        "existing_work": sorted(existing, key=lambda item: item["url"]),
        "issue_guides": guides,
    }
    if "plan_schema" in context:
        request["plan_schema"] = context["plan_schema"]
    binding = json.dumps(
        [context.get("backlog_scope"), identities, request],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    request["request_id"] = hashlib.sha256(binding).hexdigest()
    return request


def execute(command, request, timeout):
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Topic finder requires a positive finite timeout")
    deadline = time.monotonic() + timeout
    with (
        tempfile.TemporaryDirectory() as directory,
        tempfile.TemporaryFile(dir=directory) as data,
    ):
        data.write(json.dumps(request, ensure_ascii=False).encode())
        data.seek(0)
        try:
            process = subprocess.Popen(
                command,
                cwd=directory,
                stdin=data,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env={"PATH": os.defpath, "LC_ALL": "C"},
                start_new_session=True,
            )
        except OSError:
            raise ValueError("Topic finder command could not start") from None
        output = bytearray()
        try:
            with process.stdout as stream, selectors.DefaultSelector() as selector:
                selector.register(stream, selectors.EVENT_READ)
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        raise TimeoutError("Topic finder timed out")
                    chunk = os.read(stream.fileno(), 65536)
                    if not chunk:
                        break
                    if len(output) + len(chunk) > MAX_OUTPUT_BYTES:
                        raise ValueError("Topic finder output exceeds its byte limit")
                    output.extend(chunk)
            try:
                status = process.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                raise TimeoutError("Topic finder timed out") from None
            if status:
                raise ValueError("Topic finder command failed")
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            raise
    try:
        return json.loads(output)
    except (ValueError, RecursionError):
        raise ValueError("Topic finder returned invalid JSON") from None


def run_finder(command, context, records, *, limit, timeout):
    validate_command(command)
    request = build_request(context, records, limit)
    result = execute(command, request, timeout)
    if (
        not isinstance(result, dict)
        or set(result) != {"schema", "request_id", "topics", "no_action_reason"}
        or result["schema"] != PLAN_SCHEMA
        or result["request_id"] != request["request_id"]
        or not isinstance(result["topics"], list)
        or not isinstance(result["no_action_reason"], str)
    ):
        raise ValueError("Topic finder returned an invalid plan or stale request ID")
    refs = {item["ref"] for item in request["findings"]}
    urls = {item["url"].casefold() for item in request["existing_work"]}
    for topic in result["topics"]:
        if not isinstance(topic, dict):
            raise ValueError("Invalid topic in finder plan")
        selected, matches = (
            topic.get("representative_event_refs"),
            topic.get("existing_work_urls"),
        )
        if not isinstance(selected, list) or any(
            not isinstance(ref, str) or ref not in refs for ref in selected
        ):
            raise ValueError("Topic finder selected an unknown finding")
        if not isinstance(matches, list) or any(
            not isinstance(url, str) or url.casefold() not in urls for url in matches
        ):
            raise ValueError("Topic finder selected unknown existing work")
    return {"topics": result["topics"], "no_action_reason": result["no_action_reason"]}
