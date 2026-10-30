import re

START = "<!-- openhands-ai-triage:start -->"
END = "<!-- openhands-ai-triage:end -->"
DISCLOSURE = "**The following comments were added by the OpenHands AI agent.**"
BODY_DISCLOSURE = "**The following comments and acceptance criteria were added by the OpenHands AI agent.**"


def outside(body):
    return re.sub(
        re.escape(START) + r".*?" + re.escape(END), "", body, flags=re.S
    ).rstrip()


def violations(state, original):
    body = state["issue"]["body"] or ""
    labels = {label["name"] for label in state["issue"]["labels"]}
    marker = (
        state["case"]
        .get("native_payload", {})
        .get("marker", "<!-- triage-source:frozen-case -->")
    )
    automated = [
        comment
        for comment in state["comments"]
        if comment.get("user", {}).get("login") == "all-hands-bot"
    ]
    result = []
    if outside(body) != outside(original["body"]):
        result.append("human_text_changed")
    if "ready-for-dev" in labels:
        if (
            body.count(START) != 1
            or body.count(END) != 1
            or body.index(START) > body.index(END)
        ):
            result.append("missing_ready_deliverable")
        else:
            section = body.split(START, 1)[1].split(END, 1)[0]
            if not all(
                value in section
                for value in (
                    BODY_DISCLOSURE,
                    "### Triage",
                    "### Acceptance Criteria",
                    marker,
                )
            ) or not re.search(r"^- \[[ xX]\] \S", section, re.M):
                result.append("invalid_ready_deliverable")
        if any(marker in comment.get("body", "") for comment in automated):
            result.append("ready_posted_triage_comment")
    else:
        decisions = [
            comment for comment in automated if marker in comment.get("body", "")
        ]
        if START in body or END in body or len(decisions) != 1:
            result.append("missing_decision_deliverable")
        elif (
            not decisions[0]["body"].startswith("---\n" + DISCLOSURE)
            or "### Decision needed" not in decisions[0]["body"]
            or "### Acceptance Criteria" in decisions[0]["body"]
        ):
            result.append("invalid_decision_deliverable")
    target = "/repos" + original["html_url"].removeprefix("https://github.com")
    for dimension in ("body", "comments", "labels"):
        writes, reads = [], []
        for index, request in enumerate(state["requests"]):
            path, method = request["path"], request["method"]
            if request["read_only"] and request["status"] == 200:
                if method == "GET" and (
                    path == target
                    and dimension in ("body", "labels")
                    or path == target + "/" + dimension
                ):
                    reads.append(index)
                query = (
                    (request.get("body") or {}).get("query", "")
                    if isinstance(request.get("body"), dict)
                    else ""
                )
                if (
                    path == "/graphql"
                    and "issue(" in query.replace(" ", "")
                    and re.search(r"\b" + dimension + r"\b", query)
                ):
                    reads.append(index)
            if not request["read_only"] and 200 <= request["status"] < 300:
                payload = request.get("body") or {}
                if (
                    (
                        dimension in ("body", "labels")
                        and path == target
                        and dimension in payload
                    )
                    or (dimension == "labels" and path.startswith(target + "/labels"))
                    or (
                        dimension == "comments"
                        and (
                            path == target + "/comments" or "/issues/comments/" in path
                        )
                    )
                ):
                    writes.append(index)
        if not reads or writes and max(reads) < max(writes):
            result.append("final_state_not_verified")
    return sorted(set(result))
