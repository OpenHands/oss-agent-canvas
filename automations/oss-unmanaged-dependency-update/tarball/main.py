"""Deterministically update unmanaged runtime pins across OpenHands OSS repositories."""

import base64
import hashlib
import json
import os
import re
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

AGE_WINDOW = timedelta(days=7)
HTTP_TIMEOUT = 30
BRANCH = "automation/update-unmanaged-dependencies"
DISCLOSURE = "_This pull request was created by an AI agent (OpenHands) on behalf of the user._"
SDK_REPO = "OpenHands/software-agent-sdk"
SDK_DOCKERFILE = "openhands-agent-server/openhands/agent_server/docker/Dockerfile"
APP_EDITOR_REPO = "OpenHands/extensions"


@dataclass(frozen=True)
class Release:
    version: str
    published_at: datetime
    source_url: str
    metadata: dict


def _session_key() -> str:
    return os.environ.get("SESSION_API_KEY") or os.environ.get("OH_SESSION_API_KEYS_0", "")


def get_secret(name: str) -> str:
    url = os.environ.get("AGENT_SERVER_URL", "").rstrip("/")
    req = urllib.request.Request(
        f"{url}/api/settings/secrets/{name}",
        headers={"X-Session-API-Key": _session_key()},
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as response:
        return response.read().decode().strip()


def fire_callback(status: str = "COMPLETED", error: str | None = None) -> None:
    url = os.environ.get("AUTOMATION_CALLBACK_URL", "")
    if not url:
        return
    body = {"status": status, "run_id": os.environ.get("AUTOMATION_RUN_ID", "")}
    if error:
        body["error"] = error
    try:
        urllib.request.urlopen(
            urllib.request.Request(
                url,
                data=json.dumps(body).encode(),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {os.environ.get('AUTOMATION_CALLBACK_API_KEY', '')}",
                },
            ),
            timeout=HTTP_TIMEOUT,
        )
    except Exception as exc:
        print(f"Callback error: {exc}")


def report_phase(message: str) -> None:
    url = os.environ.get("AUTOMATION_PHASE_URL", "")
    token = os.environ.get("AUTOMATION_CALLBACK_API_KEY", "")
    if not url or not token:
        return
    try:
        urllib.request.urlopen(
            urllib.request.Request(
                url,
                data=json.dumps({"phase": message[:200]}).encode(),
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
            ),
            timeout=5,
        )
    except Exception:
        pass


def parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.lstrip("v").split("."))


def select_release(
    releases: list[Release], now: datetime, major: int | None = None
) -> Release | None:
    cutoff = now.astimezone(timezone.utc) - AGE_WINDOW
    eligible = [
        release
        for release in releases
        if release.published_at <= cutoff
        and (major is None or version_key(release.version)[0] == major)
    ]
    return max(eligible, key=lambda release: version_key(release.version), default=None)


def http_json(url: str, token: str | None = None) -> object:
    headers = {"Accept": "application/json", "User-Agent": "openhands-dependency-updater"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    with urllib.request.urlopen(
        urllib.request.Request(url, headers=headers), timeout=HTTP_TIMEOUT
    ) as response:
        return json.load(response)


def github_request(token: str, method: str, path: str, body: dict | None = None) -> object:
    request = urllib.request.Request(
        f"https://api.github.com{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
        raw = response.read()
        return json.loads(raw) if raw else {}


def github_releases(project: str, token: str, prefix: str = "") -> list[Release]:
    data = github_request(token, "GET", f"/repos/{project}/releases?per_page=100")
    releases = []
    for item in data:
        tag = item.get("tag_name", "")
        if item.get("draft") or item.get("prerelease") or not item.get("published_at"):
            continue
        version = tag.removeprefix(prefix).lstrip("v")
        if not re.fullmatch(r"\d+\.\d+(?:\.\d+)?", version):
            continue
        releases.append(
            Release(version, parse_timestamp(item["published_at"]), item["html_url"], item)
        )
    return releases


def python_releases() -> list[Release]:
    data = http_json("https://www.python.org/api/v2/downloads/release/?is_published=true")
    releases = []
    for item in data:
        match = re.fullmatch(r"Python (3\.13\.\d+)", item.get("name", ""))
        if not match or item.get("pre_release") or not item.get("release_date"):
            continue
        version = match.group(1)
        releases.append(
            Release(
                version,
                parse_timestamp(item["release_date"]),
                f"https://www.python.org/downloads/release/python-{version.replace('.', '')}/",
                item,
            )
        )
    return releases


def node_releases(token: str) -> list[Release]:
    return github_releases("nodejs/node", token)


def download_sha256(url: str) -> str:
    digest = hashlib.sha256()
    with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT) as response:
        while chunk := response.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def replace_exact(text: str, old: str, new: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"Expected exactly one occurrence of {old!r}, found {count}")
    return text.replace(old, new)


def replace_all_exact(text: str, old: str, new: str, expected: int) -> str:
    count = text.count(old)
    if count != expected:
        raise RuntimeError(f"Expected {expected} occurrences of {old!r}, found {count}")
    return text.replace(old, new)


def current_versions(text: str) -> dict[str, str]:
    patterns = {
        "python": r"FROM python:(\d+\.\d+\.\d+)-slim-trixie AS python-runtime",
        "node": r"FROM node:(\d+\.\d+\.\d+)-trixie-slim AS node-runtime",
        "uv": r"COPY --from=ghcr\.io/astral-sh/uv:(\d+\.\d+\.\d+) /uv /uvx /bin/",
        "acp_node": r"node-v(\d+\.\d+\.\d+)-linux-\$\{NARCH\}",
        "tini": r"releases/download/v(\d+\.\d+\.\d+)/tini-static-\$\{TARCH\}",
    }
    result = {}
    for name, pattern in patterns.items():
        matches = sorted(set(re.findall(pattern, text)))
        if len(matches) != 1:
            raise RuntimeError(f"Expected one {name} version, found {matches}")
        result[name] = matches[0]
    return result


def update_sdk_dockerfile(text: str, token: str, now: datetime) -> tuple[str, list[dict]]:
    current = current_versions(text)
    changes = []

    sources = {
        "python": select_release(python_releases(), now, 3),
        "node": select_release(node_releases(token), now, 24),
        "uv": select_release(github_releases("astral-sh/uv", token), now),
        "acp_node": select_release(node_releases(token), now, 22),
        "tini": select_release(github_releases("krallin/tini", token), now),
    }

    for name, release in sources.items():
        if not release or version_key(release.version) <= version_key(current[name]):
            continue
        old = current[name]
        new = release.version
        if name == "python":
            http_json(f"https://hub.docker.com/v2/repositories/library/python/tags/{new}-slim-trixie")
            text = replace_exact(text, f"python:{old}-slim-trixie AS python-runtime", f"python:{new}-slim-trixie AS python-runtime")
        elif name == "node":
            http_json(f"https://hub.docker.com/v2/repositories/library/node/tags/{new}-trixie-slim")
            text = replace_exact(text, f"node:{old}-trixie-slim AS node-runtime", f"node:{new}-trixie-slim AS node-runtime")
        elif name == "uv":
            text = replace_all_exact(text, f"ghcr.io/astral-sh/uv:{old}", f"ghcr.io/astral-sh/uv:{new}", 2)
            text = text.replace(f"Pin to {old} (latest at time of", f"Pin to {new} (latest eligible at time of")
        elif name == "acp_node":
            shasums_url = f"https://nodejs.org/dist/v{new}/SHASUMS256.txt"
            shasums = urllib.request.urlopen(shasums_url, timeout=HTTP_TIMEOUT).read().decode()
            checksums = {}
            for arch in ("x64", "arm64"):
                filename = f"node-v{new}-linux-{arch}.tar.xz"
                match = re.search(rf"^([0-9a-f]{{64}})  {re.escape(filename)}$", shasums, re.MULTILINE)
                if not match:
                    raise RuntimeError(f"Missing checksum for {filename}")
                checksums[arch] = match.group(1)
            text = replace_all_exact(text, f"node-v{old}-linux-${{NARCH}}", f"node-v{new}-linux-${{NARCH}}", 2)
            for arch, checksum in checksums.items():
                pattern = rf"(NARCH={arch}; NODE_SHA256=)[0-9a-f]{{64}}"
                text, count = re.subn(pattern, rf"\g<1>{checksum}", text)
                if count != 1:
                    raise RuntimeError(f"Expected one ACP Node {arch} checksum, found {count}")
        elif name == "openvscode":
            assets = {asset["name"] for asset in release.metadata.get("assets", [])}
            expected = {
                f"openvscode-server-v{new}-linux-{arch}.tar.gz"
                for arch in ("x64", "arm64", "armhf")
            }
            missing = expected - assets
            if missing:
                raise RuntimeError(
                    f"OpenVSCode Server {new} is missing artifacts: {sorted(missing)}"
                )
            text = replace_exact(
                text,
                f'ARG RELEASE_TAG="openvscode-server-v{old}"',
                f'ARG RELEASE_TAG="openvscode-server-v{new}"',
            )
        elif name == "tini":
            assets = {asset["name"]: asset["browser_download_url"] for asset in release.metadata.get("assets", [])}
            checksums = {}
            for arch in ("amd64", "arm64"):
                asset_name = f"tini-static-{arch}"
                if asset_name not in assets:
                    raise RuntimeError(f"Missing {asset_name} in tini release {new}")
                checksums[arch] = download_sha256(assets[asset_name])
            text = replace_exact(text, f"releases/download/v{old}/tini-static-${{TARCH}}", f"releases/download/v{new}/tini-static-${{TARCH}}")
            for arch, checksum in checksums.items():
                pattern = rf"(TARCH={arch}; TINI_SHA256=)[0-9a-f]{{64}}"
                text, count = re.subn(pattern, rf"\g<1>{checksum}", text)
                if count != 1:
                    raise RuntimeError(f"Expected one tini {arch} checksum, found {count}")
        changes.append({"name": name, "old": old, "new": new, "published_at": release.published_at.isoformat(), "source_url": release.source_url})
    return text, changes


def run(*args: str, cwd: Path | None = None, env: dict | None = None) -> str:
    result = subprocess.run(args, cwd=cwd, env=env, check=True, text=True, capture_output=True)
    return result.stdout.strip()


def git_env(token: str) -> dict:
    env = os.environ.copy()
    credentials = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    env["GIT_CONFIG_COUNT"] = "1"
    env["GIT_CONFIG_KEY_0"] = "http.https://github.com/.extraheader"
    env["GIT_CONFIG_VALUE_0"] = f"AUTHORIZATION: basic {credentials}"
    return env


def find_open_pr(token: str, repo: str) -> dict | None:
    owner = repo.split("/", 1)[0]
    query = urllib.parse.urlencode({"state": "open", "head": f"{owner}:{BRANCH}"})
    prs = github_request(token, "GET", f"/repos/{repo}/pulls?{query}")
    return prs[0] if prs else None


def pr_body(changes: list[dict]) -> str:
    rows = "\n".join(
        f"- **{item['name']}**: `{item['old']}` → `{item['new']}`; published `{item['published_at']}`; [official source]({item['source_url']})"
        for item in changes
    )
    return f"""<!-- Keep this PR as draft until it is ready for review. -->

<!-- AI/LLM agents:
Do not edit the HUMAN section.
-->

HUMAN:

<!--
Human author: please replace this comment with a short note (at least 20 visible
characters) before marking ready for review.
AI agents: you must not edit this section.
-->

---

AGENT:

## Why

Keep unmanaged agent-server runtime pins current after a seven-day observation window.

## Summary

{rows}

Every selected release was at least 168 hours old when this automation ran. Dependencies covered by Dependabot or dedicated updater workflows were excluded.

## Issue Number

N/A

## How to Test

- Exact Dockerfile replacement counts and official artifact/checksum availability were validated before this PR was opened.
- The updated Dockerfile was parsed again to verify every configured pin resolves to one version.
- Comprehensive image validation is delegated to repository CI.

## Video/Screenshots

Not applicable for dependency pin updates.

## Design Doc

Not applicable.

## Type

- [ ] Bug fix
- [ ] Feature
- [ ] Refactor
- [ ] Breaking change
- [x] Docs / chore

## Notes

{DISCLOSURE}
"""


def update_repository(token: str, now: datetime) -> str | None:
    report_phase(f"Checking {SDK_REPO}")
    existing_pr = find_open_pr(token, SDK_REPO)
    env = git_env(token)
    with tempfile.TemporaryDirectory() as temp_dir:
        repo_dir = Path(temp_dir) / "repo"
        run("git", "clone", "--depth", "1", f"https://github.com/{SDK_REPO}.git", str(repo_dir), env=env)
        if existing_pr:
            run("git", "fetch", "origin", f"{BRANCH}:{BRANCH}", cwd=repo_dir, env=env)
            run("git", "checkout", BRANCH, cwd=repo_dir)
            run("git", "rebase", "origin/main", cwd=repo_dir, env=env)
        else:
            run("git", "checkout", "-b", BRANCH, cwd=repo_dir)

        dockerfile = repo_dir / SDK_DOCKERFILE
        original = dockerfile.read_text()
        updated, changes = update_sdk_dockerfile(original, token, now)
        if not changes:
            print(f"{SDK_REPO}: no eligible unmanaged dependency updates")
            return None
        dockerfile.write_text(updated)
        current_versions(updated)
        run("git", "diff", "--check", cwd=repo_dir)
        run("git", "config", "user.name", "openhands", cwd=repo_dir)
        run("git", "config", "user.email", "openhands@all-hands.dev", cwd=repo_dir)
        run("git", "add", SDK_DOCKERFILE, cwd=repo_dir)
        run("git", "commit", "-m", "chore(deps): update unmanaged runtime dependencies\n\nCo-authored-by: openhands <openhands@all-hands.dev>", cwd=repo_dir)
        push_args = ["git", "push", "origin", f"HEAD:{BRANCH}"]
        if existing_pr:
            push_args.insert(2, "--force-with-lease")
        run(*push_args, cwd=repo_dir, env=env)

        body = pr_body(changes)
        title = "chore(deps): update unmanaged runtime dependencies"
        if existing_pr:
            github_request(token, "PATCH", f"/repos/{SDK_REPO}/pulls/{existing_pr['number']}", {"title": title, "body": body})
            url = existing_pr["html_url"]
        else:
            created = github_request(token, "POST", f"/repos/{SDK_REPO}/pulls", {"title": title, "head": BRANCH, "base": "main", "body": body, "draft": True})
            url = created["html_url"]
        print(f"{SDK_REPO}: {url}")
        return url


def main() -> None:
    token = get_secret("GITHUB_TOKEN")
    user = github_request(token, "GET", "/user")
    print(f"Authenticated to GitHub as {user['login']}")
    now = datetime.now(timezone.utc)
    update_repository(token, now)
    for repo in ("OpenHands/OpenHands", "OpenHands/automation", "OpenHands/extensions"):
        print(f"{repo}: no explicitly configured unmanaged exact-version pins; skipped")


if __name__ == "__main__":
    try:
        main()
        fire_callback()
    except Exception as error:
        fire_callback("FAILED", str(error))
        raise
