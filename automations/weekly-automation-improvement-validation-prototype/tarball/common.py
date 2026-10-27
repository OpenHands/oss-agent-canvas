import argparse
import datetime as dt
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


def call_model(args, model, instruction, context, name, schema):
    body = {"model": model}
    messages = [
        {"role": "system", "content": instruction},
        {"role": "user", "content": encode(context)},
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
    body = redact(body)
    encoded = encode(body).encode()
    if len(encoded) > args.max_input_bytes:
        raise ValueError(
            "Model input exceeds its byte budget; reduce the sample or evidence budget"
        )
    key = os.environ.get("LLM_API_KEY") or os.environ.get("LITELLM_API_KEY")
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
        write_json(
            args.output / "responses" / (name + "-error.json"),
            {"http_status": error.code, "seconds": time.monotonic() - started},
        )
        raise ValueError(
            f"Model endpoint returned HTTP {error.code}; no automatic retry"
        ) from None
    if len(raw) > 2_000_000:
        raise ValueError("Model response exceeds its byte budget")
    response = json.loads(raw)
    write_json(args.output / "responses" / (name + ".json"), redact(response))
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
