import argparse
import http.cookiejar
import json
from pathlib import Path
import secrets
import shutil
import urllib.request


parser = argparse.ArgumentParser()
parser.add_argument("data_dir", type=Path)
parser.add_argument("--project-id")
parser.add_argument("--workspace-id")
parser.add_argument("--cloud-key-file", type=Path, default=Path.home() / ".config/laminar/flow1-cloud.env")
args = parser.parse_args()
args.data_dir.mkdir(parents=True, exist_ok=True)
for name in ("compose.yaml", "clickhouse-profiles-config.xml", "clickhouse-server-config.xml"):
    destination = args.data_dir / name
    if destination.resolve() != Path(__file__).with_name(name).resolve():
        shutil.copyfile(Path(__file__).with_name(name), destination)
env = args.data_dir / ".env"
if not env.exists():
    with env.open("x") as file:
        env.chmod(0o600)
        for name in ("POSTGRES_PASSWORD", "SHARED_SECRET_TOKEN", "NEXTAUTH_SECRET", "AEAD_SECRET_KEY", "CLICKHOUSE_PASSWORD"):
            file.write(f"{name}={secrets.token_hex(32)}\n")
if not args.project_id or not args.workspace_id:
    print("Configuration prepared. Start Docker Compose, create the local workspace/project, then pass both IDs.")
    raise SystemExit(0)

base = "http://localhost:15667"
client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


def call(path, body=None):
    request = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", "Origin": base},
    )
    with client.open(request, timeout=30) as response:
        return json.load(response)


call("/api/auth/sign-in/local-email", {"email": "flow1-local@example.test"})
config_path = args.data_dir / "local-project.json"
if config_path.exists():
    config = json.loads(config_path.read_text())
    if config["projectId"] != args.project_id or config["workspaceId"] != args.workspace_id:
        raise ValueError("Existing local configuration belongs to another project")
else:
    key = call(
        f"/api/projects/{args.project_id}/api-keys",
        {"name": "local-synthetic-ingestion", "isIngestOnly": True, "expiresDays": 30},
    )
    config = {"projectId": args.project_id, "workspaceId": args.workspace_id, "ingestKey": key["value"]}
    with config_path.open("x") as file:
        config_path.chmod(0o600)
        json.dump(config, file)

profile_path = f"/api/workspaces/{args.workspace_id}/llm-profiles"
profiles = call(profile_path)
profile = next((item for item in profiles if item["name"] == "Laminar Cloud flow-1"), None)
if profile is None:
    cloud_key = args.cloud_key_file.read_text().strip().split("=", 1)[1]
    profile = call(profile_path, {
        "name": "Laminar Cloud flow-1",
        "provider": "custom",
        "config": {"baseUrl": "https://api.lmnr.ai/v1"},
        "secrets": {"apiKey": cloud_key},
        "models": ["flow-1"],
    })

config["profileId"] = profile["id"]
config_path.write_text(json.dumps(config))

cost_path = f"/api/projects/{args.project_id}/custom-model-costs"
existing_costs = call(cost_path)
for model in ("flow-1", "flow-1-style-r3-mtp-vonly"):
    body = {
        "provider": "openai",
        "model": model,
        "costs": {
            "input_cost_per_token": 5e-8,
            "cache_read_input_token_cost": 1e-8,
            "output_cost_per_token": 3e-7,
        },
    }
    existing = next((item for item in existing_costs if item["provider"] == "openai" and item["model"] == model), None)
    if existing:
        body["id"] = existing["id"]
    call(cost_path, body)
print("Local ingestion, flow-1 profile, and published Signals cost rates configured.")
