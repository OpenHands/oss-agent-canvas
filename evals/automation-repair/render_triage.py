import sys

from reviewer_world import REPO
from triage_world import issue


def render(source, case):
    sys.path.insert(0, str(source))
    from worker import IssueTriage

    automation = IssueTriage.__new__(IssueTriage)
    automation.repository = REPO
    automation.token_name = "GITHUB_TOKEN"
    value = issue(case)
    return automation._prompt(
        value, [], [], [value], "<!-- triage-source:frozen-case -->", None
    )
