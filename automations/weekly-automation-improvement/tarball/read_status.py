import argparse
import json
import re
import subprocess
import sys
from urllib.parse import urlencode

from backlog import CUSTOMER, EXTERNAL, FACTORY
from github_io import command, github_pages
from public_status import LOGIN, REPO, ROLES, managed_block, read_marker

ROUTES = {
    CUSTOMER: "<!-- automation-improvement-customer: 1 -->",
    FACTORY: "<!-- automation-improvement-factory: 1 -->",
}
CONTENT_QUERY = """
fragment IssueContext on Issue {
  url
  state
  labels(first: 100) { nodes { name } pageInfo { hasNextPage } }
}
query($id: ID!) {
  node(id: $id) {
    ... on Comment {
      author { __typename login }
      editor { __typename login }
      lastEditedAt
      body
    }
    ... on Issue { ...IssueContext }
    ... on IssueComment { url issue { ...IssueContext } }
  }
}
"""


def github_node(node_id):
    try:
        data = json.loads(
            command(
                [
                    "gh",
                    "api",
                    "--hostname",
                    "github.com",
                    "graphql",
                    "-f",
                    "query=" + CONTENT_QUERY,
                    "-f",
                    "id=" + node_id,
                ]
            )
        )
    except subprocess.SubprocessError as error:
        raise ValueError("GitHub could not verify status edit provenance") from error
    if not data.get("errors"):
        return (data.get("data") or {}).get("node")


def actor_login(actor):
    if not actor or actor.get("__typename") not in {"Bot", "User", "Organization"}:
        return None
    name = (actor.get("login") or "").casefold()
    if name and actor["__typename"] == "Bot" and not name.endswith("[bot]"):
        return name + "[bot]"
    return name or None


def verified_publisher(row, url, issue, publishers, lookup, pages):
    node_id = row.get("node_id")
    node = lookup(node_id) if isinstance(node_id, str) and node_id else None
    if (
        not node
        or node.get("url") != url
        or node.get("body") != row["body"]
        or "lastEditedAt" not in node
        or "editor" not in node
    ):
        raise ValueError("Could not verify the current GitHub status text and editor")
    current = node.get("issue", node)
    if (
        not current
        or current.get("url") != issue["html_url"]
        or current.get("state") != issue["state"].upper()
        or not current.get("labels")
    ):
        raise ValueError("Could not verify the current status issue and state")
    labels = current["labels"]["nodes"]
    if current["labels"]["pageInfo"]["hasNextPage"]:
        endpoint = "repos/" + issue["html_url"].removeprefix("https://github.com/")
        labels = pages(endpoint + "/labels?per_page=100")
    if {r["name"] for r in labels} != {r["name"] for r in issue["labels"]}:
        raise ValueError("Issue labels changed while reading the status")
    author = actor_login(node.get("author"))
    if author != row["user"]["login"].casefold() or author not in publishers:
        raise ValueError("Could not verify the GitHub status author")
    if node["lastEditedAt"] is None and node["editor"] is None:
        return author
    editor = actor_login(node["editor"])
    if editor not in publishers:
        raise ValueError("The latest GitHub status editor is unknown or untrusted")
    return editor


def status_of(issue, endpoint, publishers, pages=github_pages, lookup=github_node):
    url = issue["html_url"]
    if (
        url.casefold()
        != f"https://github.com/{endpoint.removeprefix('repos/')}".casefold()
    ):
        raise ValueError("Issue is outside the requested repository or issue number")
    labels = {row["name"] for row in issue["labels"]}
    routes = {marker for label, marker in ROUTES.items() if label in labels}
    owned = []
    notices = []
    comments = (
        [] if issue.get("comments") == 0 else pages(endpoint + "/comments?per_page=100")
    )
    for row in comments:
        author = (row.get("user") or {}).get("login", "").casefold()
        text = row.get("body") or ""
        route = text.partition("\n")[0]
        if author not in publishers or route not in ROUTES.values():
            continue
        try:
            record = read_marker(text)
        except ValueError:
            if route in routes:
                raise
            continue
        if not record:
            continue
        result = (
            row,
            record
            | {
                "issue_url": url,
                "status_url": f"{url}#issuecomment-{row['id']}",
            },
        )
        if route in routes or not routes:
            if record["status"] == "ownership_conflict" or (
                (not routes or EXTERNAL not in labels)
                and record["status"] == "enrollment_removed"
            ):
                notices.append(result)
            elif route in routes and len(routes) == 1 and EXTERNAL in labels:
                owned.append(result)
        elif record["status"] == "ownership_conflict":
            notices.append(result)
    matches = notices or owned
    if len(matches) > 1:
        raise ValueError(f"Multiple trusted status comments for {url}")
    if matches:
        source, record = matches[0]
    else:
        author = (issue.get("user") or {}).get("login", "").casefold()
        if routes or author not in publishers or not (bounds := managed_block(issue)):
            return None
        record = read_marker(issue["body"][slice(*bounds)])
        expected_owner = "external" if EXTERNAL in labels else "native"
        if not record or record["owner"] != expected_owner:
            return None
        source, record = issue, record | {"issue_url": url, "status_url": url}
    publisher = verified_publisher(
        source, record["status_url"], issue, publishers, lookup, pages
    )
    return record | {"publisher": publisher}


def main():
    parser = argparse.ArgumentParser(
        description="Read published improvement handoffs from GitHub as JSON."
    )
    parser.add_argument("repository", metavar="OWNER/REPO")
    parser.add_argument(
        "--publisher",
        action="append",
        required=True,
        metavar="LOGIN",
        help="Trusted original author or editor of a status record (repeatable)",
    )
    parser.add_argument("--issue", type=int, metavar="N")
    parser.add_argument(
        "--label",
        action="append",
        default=[],
        help="Filter open issues by all specified labels",
    )
    parser.add_argument("--next-actor", choices=ROLES)
    args = parser.parse_args()
    if not re.fullmatch(REPO, args.repository):
        parser.error("Supply a GitHub owner/repository")
    if any(not LOGIN.fullmatch(name) for name in args.publisher):
        parser.error("Supply GitHub login names for trusted publishers")
    if args.issue is None and not args.label:
        parser.error("Supply --issue or at least one --label")
    if args.issue is not None and (args.issue < 1 or args.label):
        parser.error("Use a positive issue number separately from label filters")
    endpoint = f"repos/{args.repository}/issues"
    if args.issue is not None:
        issues = [
            json.loads(
                command(
                    [
                        "gh",
                        "api",
                        "--hostname",
                        "github.com",
                        f"{endpoint}/{args.issue}",
                    ]
                )
            )
        ]
    else:
        query = urlencode(
            {"state": "open", "labels": ",".join(args.label), "per_page": 100}
        )
        issues = github_pages(f"{endpoint}?{query}", github_request)
    publishers = {name.casefold() for name in args.publisher}
    rows = []
    errors = []
    for issue in issues:
        if "pull_request" in issue:
            continue
        try:
            row = status_of(
                issue,
                f"{endpoint}/{args.issue if args.issue is not None else issue['number']}",
                publishers,
                lambda url: github_pages(url, github_request),
            )
        except ValueError as error:
            errors.append(f"{issue['html_url']}: {error}")
            continue
        except subprocess.SubprocessError:
            errors.append(f"{issue['html_url']}: GitHub could not read the handoff")
            continue
        if row and (args.next_actor is None or row["next_actor"] == args.next_actor):
            rows.append(row)
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    if errors:
        sys.exit("\n".join(errors))


def github_request(args):
    return command([*args[:2], "--hostname", "github.com", *args[2:]])


if __name__ == "__main__":
    main()
