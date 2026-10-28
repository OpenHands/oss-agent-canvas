import json
import sys
from pathlib import Path

from reviewer_world import REPO, pull_request


def render(source, case):
    sys.path.insert(0, str(source))
    import worker

    builder = worker.workflow._build_review_prompt

    def build(*args, **kwargs):
        kwargs["workspace_instructions"] = (
            "The workspace is already the repository root at the exact Head SHA above, "
            "with the PR base and head available locally. Do not clone, fetch, check out, "
            "or delete the repository."
        )
        return builder(*args, **kwargs)

    worker.workflow._build_review_prompt = build
    reviewer = worker.PullRequestReviewer.__new__(worker.PullRequestReviewer)
    reviewer.repository = REPO
    reviewer.token_name = "GITHUB_TOKEN"
    reviewer.config = {"trigger_reviewer": "all-hands-bot"}
    pr = pull_request()
    pr["head"]["sha"] = case["requested_sha"]
    pr["base"]["sha"] = case.get("base_sha", pr["base"]["sha"])
    return reviewer._prompt(pr, {"id": 1, "created_at": "2026-10-27T00:00:00Z"})


if __name__ == "__main__":
    print(
        json.dumps(
            {
                "prompt": render(
                    Path(sys.argv[1]), json.loads(Path(sys.argv[2]).read_text())
                )
            }
        )
    )
