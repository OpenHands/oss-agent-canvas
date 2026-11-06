import json
from types import SimpleNamespace
import sys
from test_automation import TARBALL

sys.path.insert(0, str(TARBALL))

import repository_snapshot


def test_related_comments_are_fully_captured_and_cached(monkeypatch):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if "/comments?" in argv[2]:
            assert "--paginate" in argv and "--slurp" not in argv
            value = [[{"id": i} for i in range(100)], [{"id": 100}]]
            return SimpleNamespace(
                returncode=0,
                stdout="\n".join(json.dumps(page) for page in value),
                stderr="",
            )
        else:
            value = {"number": 1, "body": "Original body"}
        return SimpleNamespace(returncode=0, stdout=json.dumps(value), stderr="")

    monkeypatch.setattr(repository_snapshot.subprocess, "run", run)
    native = {
        "repository": "Example/project",
        "task": {"issue": {"number": 1, "author_body": "Original body"}},
    }
    cache = {}
    first = repository_snapshot.capture_reads(native, cache)
    second = repository_snapshot.capture_reads(native, cache)
    assert first == second and len(calls) == 2
    assert len(first["/repos/Example/project/issues/1/comments"]["body"]) == 101
