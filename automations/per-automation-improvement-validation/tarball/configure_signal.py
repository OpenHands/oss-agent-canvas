import argparse
import uuid
from pathlib import Path
from types import SimpleNamespace

from common import encode, read_json
from flow1 import signal_definition
from laminar import Laminar
from weekly import load_environment


def configure_rate(client, project_id, signal_id, sample_rate):
    if sample_rate is not None and (
        type(sample_rate) is not int or not 1 <= sample_rate <= 95
    ):
        raise ValueError(
            "Sample rate must be an integer percentage from 1 to 95, or None for all traces"
        )
    identifier = str(uuid.UUID(signal_id))
    url = client.base + "/v1/signals/" + identifier
    before = signal_definition(client.request(url), project_id)
    if before["id"] != identifier:
        raise ValueError("Returned Signal differs from the configured ID")
    if before.get("sampleRate") != sample_rate:
        client.request(url, {"sampleRate": sample_rate}, method="PATCH")
    after = signal_definition(client.request(url), project_id)
    preserved = (
        "id",
        "projectId",
        "trigger",
        "filters",
        "disabled",
        "prompt",
        "structuredOutput",
        "llmProfileId",
        "model",
        "mode",
    )
    if after.get("sampleRate") != sample_rate or any(
        before.get(key) != after.get(key) for key in preserved
    ):
        raise ValueError(
            "Signal configuration verification failed; inspect live settings"
        )
    return {
        "signal_id": identifier,
        "previous_sample_rate": before.get("sampleRate"),
        "sample_rate": after["sampleRate"],
        "version": after["version"],
        "disabled": after["disabled"],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config",
        type=Path,
        default=Path.home() / ".config/automation-improvement/config.json",
    )
    sampling = parser.add_mutually_exclusive_group(required=True)
    sampling.add_argument("--sample-rate", type=int, metavar="PERCENT")
    sampling.add_argument("--no-sampling", action="store_true")
    args = parser.parse_args()
    config = read_json(args.config)
    load_environment(config)
    result = configure_rate(
        Laminar(SimpleNamespace(max_response_bytes=1_000_000)),
        config["project_id"],
        config["signal_id"],
        args.sample_rate,
    )
    print(encode(result))


if __name__ == "__main__":
    main()
