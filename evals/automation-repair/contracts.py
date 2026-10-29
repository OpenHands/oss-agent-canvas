import json
import re

from scenarios import TRIAGE

REVIEWER = "plugins/github-pr-reviewer-events/"
ISSUE_TRIAGE = "plugins/issue-triage/"
FACTS = ("review_body_contradiction", "review_body_absence_control")
PUBLICATION = (
    "healthy",
    "buggy",
    "head_moved",
    "head_moves_late",
    "body_file_failure",
    "accepted_empty",
)


def select(request):
    paths = request["changed_files"]
    context = json.dumps(
        [request.get("topic"), request.get("findings"), request.get("hypothesis", "")]
    ).lower()
    if paths and all(path.startswith(ISSUE_TRIAGE) for path in paths):
        if re.search(r"priorit|user.pain|reported.harm|reproduc|readiness", context):
            return "issue_priority", list(TRIAGE)
    if paths and all(path.startswith(REVIEWER) for path in paths):
        if re.search(
            r"claims?|factual|contradict|metadata|description|test.instructions",
            context,
        ):
            return "review_faithfulness", list(FACTS)
        if re.search(
            r"publication|publish|duplicate|wrong.head|moved.head|commit_id|body.file",
            context,
        ):
            return "review_publication", list(PUBLICATION)
    return None, []


def captured_inputs(snapshot, contract):
    if contract != "issue_priority":
        return {}
    for sample in snapshot.get("samples", []):
        for message in sample.get("initial_instructions", []):
            content = message.get("content", "")
            text = (
                content
                if isinstance(content, str)
                else "\n".join(
                    part.get("text", "") for part in content if isinstance(part, dict)
                )
            )
            start = text.rfind('\n\n{"issue":')
            if start < 0:
                continue
            payload = json.loads(text[start + 2 :])
            body = payload["issue"].get("author_body", "")
            if body:
                return {"reported_paid_harm": {"body": body}}
    return {}
