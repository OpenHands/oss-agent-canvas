import json
import sys
from collections import defaultdict


def timeout_representatives(findings):
    linked = defaultdict(set)
    for finding in findings:
        first, *others = finding["unit_refs"]
        for alias in others:
            linked[first].add(alias)
            linked[alias].add(first)

    seen = set()
    representatives = []
    for finding in findings:
        unit = finding["unit_refs"][0]
        if finding["group"] != "report-timeout" or unit in seen:
            continue
        representatives.append(finding["ref"])
        pending = [unit]
        while pending:
            alias = pending.pop()
            if alias not in seen:
                seen.add(alias)
                pending.extend(linked[alias])
    return representatives


def find_topics(request):
    if request["schema"] != "openhands.topic-finder-input.v1":
        raise ValueError("Unsupported topic finder input schema")
    refs = timeout_representatives(request["findings"])
    topics = []
    if len(refs) >= 2:
        topics.append(
            {
                "key": "report-timeout",
                "title": "Investigate recurring report timeouts",
                "reason": "Report requests reached their deadline in multiple independent runs.",
                "priority": 2,
                "representative_event_refs": refs[: request["representative_limit"]],
                "existing_work_urls": [],
                "brief": {
                    "problem": "Callers expect a completed report within the deadline. A timeout leaves them without the requested result.",
                    "example": "A caller waited for a report until its deadline expired.",
                    "prevention": "Measure how long report generation and delivery each take to identify which step needs a change.",
                    "validation": "Replay the affected requests and check that each returns a complete report within the deadline. Confirm timely requests still succeed.",
                },
            }
        )
    return {
        "schema": "openhands.topic-plan.v1",
        "request_id": request["request_id"],
        "topics": topics,
        "no_action_reason": ""
        if topics
        else "Fewer than two independent report timeouts.",
    }


if __name__ == "__main__":
    print(json.dumps(find_topics(json.load(sys.stdin))))
