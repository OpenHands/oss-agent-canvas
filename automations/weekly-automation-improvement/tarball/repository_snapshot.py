import hashlib
import json
import re
import subprocess
import tarfile
from pathlib import PurePosixPath
from urllib.parse import quote

from common import write_json
from patches import command


def capture(repository, output):
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repository):
        raise ValueError("Invalid task repository")
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    metadata = json.loads(command(["gh", "api", f"repos/{repository}"]))
    commit = json.loads(
        command(
            [
                "gh",
                "api",
                f"repos/{repository}/commits/"
                + quote(metadata["default_branch"], safe=""),
            ]
        )
    )
    revision = commit["sha"]
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("Repository revision is not pinned")
    archive = output / (revision + ".tar.gz")
    with archive.open("xb") as stream:
        archive.chmod(0o600)
        subprocess.run(
            ["gh", "api", f"repos/{repository}/tarball/{revision}"],
            stdout=stream,
            stderr=subprocess.PIPE,
            check=True,
            timeout=180,
        )
    files, tree, total = {}, [], 0
    with tarfile.open(archive) as bundle:
        for member in bundle:
            parts = PurePosixPath(member.name).parts
            if len(parts) < 2:
                continue
            name = PurePosixPath(*parts[1:])
            if name.is_absolute() or ".." in name.parts or ".git" in name.parts:
                raise ValueError("Unsafe repository archive path")
            if member.isdir():
                continue
            if not member.isfile():
                tree.append(
                    {"path": str(name), "type": "unsupported", "mode": "120000"}
                )
                continue
            total += member.size
            if total > 500_000_000 or len(tree) >= 100_000:
                raise ValueError("Repository archive exceeds the replay budget")
            data = bundle.extractfile(member).read()
            tree.append(
                {
                    "path": str(name),
                    "type": "blob",
                    "mode": "100755" if member.mode & 0o111 else "100644",
                    "size": member.size,
                    "sha": hashlib.sha1(
                        b"blob " + str(len(data)).encode() + b"\0" + data
                    ).hexdigest(),
                }
            )
            if (
                str(name) in ("AGENTS.md", "CONTRIBUTING.md", "README.md")
                and len(data) <= 200_000
            ):
                files[str(name)] = data.decode(errors="replace")
    tree_response = json.loads(
        command(
            [
                "gh",
                "api",
                f"repos/{repository}/git/trees/{commit['commit']['tree']['sha']}?recursive=1",
            ]
        )
    )
    if tree_response.get("truncated"):
        raise ValueError("Repository tree was truncated")
    index = output / (revision + "-index.json")
    write_json(
        index, {"tree": tree_response["tree"], "metadata": metadata, "commit": commit}
    )
    return {
        "repository": repository,
        "revision": revision,
        "files": files,
        "archive_path": str(archive.resolve()),
        "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "index_path": str(index.resolve()),
        "index_sha256": hashlib.sha256(index.read_bytes()).hexdigest(),
        "scope": "Pinned present-day repository, paired with original task data; not a reconstruction of historical repository state.",
    }


def capture_reads(native, cache):
    repository = native["repository"]
    task = native["task"]
    text = (
        task["issue"]["author_body"]
        + "\n"
        + "\n".join(item["body"] for item in task.get("human_discussion", []))
    )
    references = {
        (repository, number) for number in re.findall(r"(?<!\w)#(\d{1,9})", text)
    }
    references.update(
        re.findall(r"https://github.com/([\w.-]+/[\w.-]+)/(?:issues|pull)/(\d+)", text)
    )
    references.add((repository, str(task["issue"]["number"])))
    if len(references) > 40:
        raise ValueError("Related GitHub context exceeds the evidence budget")
    responses = {}
    for repo, number in sorted(references):
        root = f"/repos/{repo}/issues/{number}"
        for endpoint in (root, root + "/comments"):
            if endpoint not in cache:
                result = subprocess.run(
                    ["gh", "api", endpoint + "?per_page=100", "--paginate", "--slurp"]
                    if endpoint.endswith("/comments")
                    else ["gh", "api", endpoint],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                if result.returncode and "HTTP 404" not in result.stderr:
                    raise ValueError("Related GitHub context could not be captured")
                value = json.loads(result.stdout)
                if endpoint.endswith("/comments") and not result.returncode:
                    value = [item for page in value for item in page]
                cache[endpoint] = {
                    "status": 404 if result.returncode else 200,
                    "body": value,
                }
            responses[endpoint] = cache[endpoint]
        issue = responses[root]["body"]
        if isinstance(issue, dict) and issue.get("pull_request"):
            endpoint = f"/repos/{repo}/pulls/{number}"
            if endpoint not in cache:
                cache[endpoint] = {
                    "status": 200,
                    "body": json.loads(command(["gh", "api", endpoint])),
                }
            responses[endpoint] = cache[endpoint]
    return responses
