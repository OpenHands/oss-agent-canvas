import hashlib
import io
import json
import os
import shlex
import tarfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import PurePosixPath

from common import redact
from patches import command


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def native_request(base, path, limit=1_000_000, *, expose_secrets=False):
    parsed = urllib.parse.urlparse(base)
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1", "::1")
    ):
        raise ValueError("Native service requires HTTPS or a loopback URL")
    key = os.environ.get("OH_SESSION_API_KEYS_0") or os.environ.get("SESSION_API_KEY")
    if not key:
        raise ValueError("Native service session credential is unavailable")
    headers = {"X-Session-API-Key": key}
    if expose_secrets:
        headers["X-Expose-Secrets"] = "plaintext"
    request = urllib.request.Request(base.rstrip("/") + path, headers=headers)
    opener = urllib.request.build_opener(NoRedirect())
    with opener.open(request, timeout=30) as response:
        raw = response.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("Native response exceeds its byte budget")
    return raw


def github_auth(config):
    if not os.environ.get("GH_TOKEN"):
        origin = os.environ.get("AGENT_SERVER_URL", "")
        name = config.get("github_token_secret", "GITHUB_TOKEN")
        if not name.replace("_", "").isalnum():
            raise ValueError("Invalid configured GitHub secret name")
        token = (
            native_request(origin, "/api/settings/secrets/" + name, 16000)
            .decode()
            .strip()
        )
        if not token:
            raise ValueError("GitHub credential is empty")
        os.environ["GH_TOKEN"] = token
    os.environ.update(
        {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "credential.helper",
            "GIT_CONFIG_VALUE_0": "!gh auth git-credential",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )


def repository_index(checkout, revision):
    directories = {}
    listing = command(
        ["git", "ls-tree", "-rz", revision, "--", "automations/", "plugins/"],
        checkout,
        strip=False,
    )
    for entry in listing.split("\0"):
        if not entry:
            continue
        header, path = entry.split("\t", 1)
        mode, _, digest = header.split()
        parts = PurePosixPath(path).parts
        if mode not in ("100644", "100755") or len(parts) < 3:
            continue
        if parts[0] == "automations" and (len(parts) < 4 or parts[2] != "tarball"):
            continue
        if PurePosixPath(path).suffix not in (
            ".py",
            ".txt",
            ".md",
            ".json",
            ".yaml",
            ".yml",
            ".sh",
        ):
            continue
        directory = "/".join(parts[:2])
        relative = "/".join(parts[3:] if parts[0] == "automations" else parts[2:])
        directories.setdefault(directory, {})[relative] = (path, digest)
    return directories


def resolve_source(config, sample, index):
    identifier = str(uuid.UUID(sample["automation_id"]))
    base = config["automation_api_url"]
    metadata = json.loads(native_request(base, "/v1/" + identifier))
    if metadata.get("plugin_spec"):
        plugin = metadata["plugin_spec"]["plugin"]
        directory = plugin.get("repo_path", "")
        if (
            plugin.get("source") != "github:" + config["repository"]
            or plugin.get("ref", "main") != config.get("base_branch", "main")
            or len(PurePosixPath(directory).parts) != 2
            or not directory.startswith("plugins/")
            or directory not in index
        ):
            raise ValueError(
                "Plugin source is outside the approved repository, branch or path"
            )
        return {
            "directory": directory,
            "files": sorted(path for path, _ in index[directory].values()),
            "automation_name": metadata["name"],
            "provenance": "declared_plugin_source; historical executed revision not verified",
            "plugin": plugin,
            "configuration": redact(metadata["plugin_spec"].get("config", {})),
            "entrypoint": metadata.get("entrypoint"),
        }
    raw = native_request(base, "/v1/" + identifier + "/tarball")
    bundle = {}
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:*") as archive:
        members = archive.getmembers()
        if len(members) > 200 or sum(item.size for item in members) > 8_000_000:
            raise ValueError("Automation bundle exceeds its source budget")
        for item in members:
            path = PurePosixPath(item.name)
            if not item.isfile() or path.is_absolute() or ".." in path.parts:
                continue
            name = str(path)
            if name in bundle:
                raise ValueError("Duplicate source file in automation bundle")
            data = archive.extractfile(item).read()
            bundle[name] = (
                hashlib.sha1(
                    b"blob " + str(len(data)).encode() + b"\0" + data
                ).hexdigest(),
                len(data),
            )
    entrypoints = {
        PurePosixPath(part).name
        for part in shlex.split(metadata["entrypoint"])
        if part.endswith(".py")
    }
    entrypoints.update(("main.py", "prompt.txt"))
    core = {name for name in bundle if PurePosixPath(name).suffix in (".py", ".txt")}
    candidates = []
    for directory, files in index.items():
        matched = [
            name
            for name, (_, digest) in files.items()
            if name in bundle and bundle[name][0] == digest
        ]
        if not core or not core <= set(matched):
            continue
        if not any(PurePosixPath(name).name in entrypoints for name in matched):
            continue
        score = sum(bundle[name][1] for name in matched)
        candidates.append((score, directory, matched))
    candidates.sort(reverse=True)
    if not candidates or (len(candidates) > 1 and candidates[0][0] == candidates[1][0]):
        raise ValueError("Deployed bundle has no unique approved source match")
    _, directory, matched = candidates[0]
    files = [index[directory][name][0] for name in matched]
    return {
        "directory": directory,
        "files": files,
        "matched_deployed_files": matched,
        "automation_name": metadata["name"],
    }
