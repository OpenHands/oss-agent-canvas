import argparse
import datetime as dt
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

SECRET = re.compile(
    r"\bsk-[\w-]{8,}|gh[pousr]_[\w]{15,}|github_pat_[\w]{15,}|xox[baprs]-[\w-]+|Bearer\s+\S+|(?<![\w/.-])[A-Za-z0-9_-]{48,}(?![\w/.-])"
)

ASSIGNMENT = re.compile(
    r"(?i)((?:api[_-]?key|password|client_secret|[a-z_]*token)\s*[=:]\s*[\x22\x27]?)([^\s\x22\x27,;]{8,})"
)


def redact(value):
    if isinstance(value, dict):
        return {
            key: "[REDACTED]"
            if key.lower()
            in (
                "api_key",
                "authorization",
                "secret_registry",
                "password",
                "credentials",
            )
            or key.lower().endswith(("_api_key", "_secret_key", "_secret", "_token"))
            else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        for name, secret in os.environ.items():
            if re.search(r"KEY|TOKEN|SECRET|PASSWORD", name) and len(secret) >= 8:
                value = value.replace(secret, "[REDACTED]")
        value = SECRET.sub("[REDACTED]", value)
        value = ASSIGNMENT.sub(
            lambda match: (
                match.group(0)
                if re.fullmatch(
                    r"\$\{[A-Za-z_][A-Za-z0-9_]*\}|\$[A-Za-z_][A-Za-z0-9_]*",
                    match.group(2),
                )
                else match.group(1) + "[REDACTED]"
            ),
            value,
        )
    return value


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def topic_id(automation_id, key):
    if (
        not isinstance(key, str)
        or len(key) > 40
        or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", key)
    ):
        raise ValueError("Topic keys must be short lowercase kebab-case identifiers")
    return hashlib.sha256(encode([automation_id, key]).encode()).hexdigest()[:20]


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with os.fdopen(
        os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w"
    ) as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)


def read_json(path, limit=8_000_000):
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Input file exceeds its byte limit")
    return json.loads(data)


def timestamp(value):
    result = dt.datetime.fromisoformat(str(value))
    return (
        result.replace(tzinfo=dt.UTC)
        if result.tzinfo is None
        else result.astimezone(dt.UTC)
    )


def audit_response(response):
    audit = redact(response)
    pairs = list(zip(response.get("output", []), audit.get("output", [])))
    for old, new in zip(response.get("choices", []), audit.get("choices", [])):
        pairs.extend(
            (first["function"], second["function"])
            for first, second in zip(
                old.get("message", {}).get("tool_calls", []),
                new.get("message", {}).get("tool_calls", []),
            )
        )
    for original, sanitized in pairs:
        if isinstance(original.get("arguments"), str):
            try:
                sanitized["arguments"] = encode(
                    redact(json.loads(original["arguments"]))
                )
            except ValueError:
                pass
    return audit


def request_model(args, model, body, endpoint, name, *, redact_body=True):
    budget = getattr(args, "budget", None)
    if budget:
        budget.check()
    body = redact(body) if redact_body else body
    encoded = encode(body).encode()
    if len(encoded) > args.max_input_bytes:
        raise ValueError(
            "Model input exceeds its byte budget; reduce the sample or evidence budget"
        )
    key = os.environ.get("LITELLM_API_KEY") or os.environ.get("LLM_API_KEY")
    if not key:
        raise ValueError("LLM_API_KEY or LITELLM_API_KEY is required for --analyze")
    base = os.environ.get("LLM_BASE_URL") or os.environ.get("BASE_URL")
    if not base or urllib.parse.urlparse(base).scheme not in ("http", "https"):
        raise ValueError("Set an explicit LLM_BASE_URL")
    write_json(args.output / "requests" / (name + ".json"), body)
    started = time.monotonic()
    request = urllib.request.Request(
        base.rstrip("/").removesuffix("/v1") + endpoint,
        data=encoded,
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read(2_000_001)
            headers = {key.lower(): value for key, value in response.headers.items()}
    except urllib.error.HTTPError as error:
        if budget:
            budget.record((error.headers or {}).get("x-litellm-response-cost"))
        write_json(
            args.output / "responses" / (name + "-error.json"),
            {"http_status": error.code, "seconds": time.monotonic() - started},
        )
        raise ValueError(
            f"Model endpoint returned HTTP {error.code}; no automatic retry"
        ) from None
    except OSError:
        if budget:
            budget.record(None)
        raise
    if budget:
        budget.record(headers.get("x-litellm-response-cost"))
    if len(raw) > 2_000_000:
        raise ValueError("Model response exceeds its byte budget")
    response = json.loads(raw)
    write_json(args.output / "responses" / (name + ".json"), audit_response(response))
    measurement = {
        "model": response.get("model", model),
        "api": args.api,
        "input_bytes": len(encoded),
        "seconds": round(time.monotonic() - started, 3),
        "usage": response.get("usage"),
        "reported_cost_usd": headers.get("x-litellm-response-cost"),
        "effort": args.effort if args.api == "responses" else "provider_default",
    }
    write_json(args.output / "measurements" / (name + ".json"), measurement)
    return response, measurement


def call_model(args, model, instruction, context, name, schema):
    reader = getattr(args, "evidence_reader", None) if name == "coordinator" else None
    if reader is not None:
        from evidence_agent import investigate

        return investigate(args, model, instruction, context, name, schema, reader)
    budget = getattr(args, "budget", None)
    if budget:
        budget.check()
    body = {"model": model}
    messages = [
        {"role": "system", "content": redact(instruction)},
        {"role": "user", "content": encode(redact(context))},
    ]
    if args.api == "responses":
        body.update(
            input=messages,
            tools=[{"type": "function", "name": "report", "parameters": schema}],
            tool_choice={"type": "function", "name": "report"},
            max_output_tokens=args.max_output_tokens,
            reasoning={"effort": args.effort},
        )
        endpoint = "/v1/responses"
    else:
        body.update(
            messages=messages,
            tools=[
                {
                    "type": "function",
                    "function": {"name": "report", "parameters": schema},
                }
            ],
            tool_choice={"type": "function", "function": {"name": "report"}},
            max_tokens=args.max_output_tokens,
        )
        endpoint = "/v1/chat/completions"
    response, measurement = request_model(
        args, model, body, endpoint, name, redact_body=False
    )
    if args.api == "responses":
        if response.get("status") == "incomplete":
            raise ValueError("Model exhausted its output budget")
        call = next(
            (
                item
                for item in response.get("output", [])
                if item.get("type") == "function_call" and item.get("name") == "report"
            ),
            None,
        )
        if call is None:
            raise ValueError("Model returned no report function call")
        result = json.loads(call["arguments"])
    else:
        choice = response["choices"][0]
        if choice.get("finish_reason") == "length":
            raise ValueError("Model exhausted its output budget")
        result = json.loads(choice["message"]["tool_calls"][0]["function"]["arguments"])
    return redact(result), measurement


def bounded(minimum, maximum):
    def parse(value):
        result = int(value)
        if not minimum <= result <= maximum:
            raise argparse.ArgumentTypeError(f"Expected {minimum}..{maximum}")
        return result

    return parse
