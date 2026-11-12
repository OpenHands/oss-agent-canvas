from triage_contract import START, END, DISCLOSURE, BODY_DISCLOSURE


def deliver(world, ready=False):
    target = f"/repos/{world.repository}/issues/{world.issue['number']}"
    marker = world.case.get("native_payload", {}).get(
        "marker", "<!-- triage-source:frozen-case -->"
    )
    if ready:
        body = (
            world.case["body"]
            + f"\n\n{START}\n{BODY_DISCLOSURE}\n### Triage\nBounded scope.\n### Acceptance Criteria\n- [ ] Observable result matches the requested behavior.\n{marker}\n{END}"
        )
        world.request("PATCH", target, {"body": body})
        world.request("POST", target + "/labels", {"labels": ["ready-for-dev"]})
    else:
        world.request(
            "POST",
            target + "/comments",
            {
                "body": f"---\n{DISCLOSURE}\n### Decision needed\nWhich product behavior should be chosen?\n{marker}"
            },
        )
    for suffix in ("", "/comments", "/labels"):
        world.request("GET", target + suffix)
