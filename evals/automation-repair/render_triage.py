import sys
import json

from reviewer_world import REPO
from triage_world import issue


def render(source, case):
    sys.path.insert(0, str(source))
    from worker import IssueTriage

    automation = IssueTriage.__new__(IssueTriage)
    native = case.get("native_payload")
    automation.repository = native["repository"] if native else REPO
    automation.token_name = "GITHUB_TOKEN"
    value = issue(case)
    if native:
        task = native["task"]
        discussion = [
            {**comment, "user": {"login": comment["author"]}}
            for comment in task.get("human_discussion", [])
        ]
        automated = task.get("automated_triage_comments", [])
        backlog = [
            {**item, "labels": [{"name": label} for label in item.get("labels", [])]}
            for item in task.get("backlog", [])
        ]
        prompt = automation._prompt(
            value,
            discussion,
            automated,
            backlog,
            native["marker"],
            task.get("readiness_check"),
        )
        start = prompt.rfind('\n\n{"issue":')
        return prompt[:start] + "\n\n" + json.dumps(task)
    return automation._prompt(
        value, [], [], [value], "<!-- triage-source:frozen-case -->", None
    )
