import json
import subprocess

IMPROVEMENT_LABEL = "automation-improvement"


def command(argv, cwd=None, strip=True, **kwargs):
    output = subprocess.run(
        argv, cwd=cwd, capture_output=True, text=True, check=True, timeout=60, **kwargs
    ).stdout
    return output.strip() if strip else output


def github_pages(endpoint, request=command):
    return json_pages(request(["gh", "api", "--paginate", endpoint]))


def json_pages(text):
    pending = text.strip()
    decoder, rows = json.JSONDecoder(), []
    while pending:
        page, end = decoder.raw_decode(pending)
        if not isinstance(page, list):
            raise ValueError("Expected a GitHub collection page")
        rows.extend(page)
        pending = pending[end:].lstrip()
    return rows


def repository_private(repository, request):
    value = json.loads(request(["gh", "api", "repos/" + repository]))
    if value.get("visibility") in ("public", "private", "internal"):
        return value["visibility"] != "public"
    if type(value.get("private")) is bool:
        return value["private"]
    raise ValueError("Repository visibility is unavailable")


def work_item(row, repository):
    return {
        "url": row["html_url"],
        "repository": repository,
        "title": row["title"],
        "body": row.get("body") or "",
        "state": row["state"].upper(),
        "createdAt": row.get("created_at"),
        "kind": "pull" if "pull_request" in row else "issue",
    }
