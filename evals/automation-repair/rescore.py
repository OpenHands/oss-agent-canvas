import argparse
import hashlib
import json
from pathlib import Path

from compare import compare, write
from scenarios import scenario, score


def rescore(source, output):
    manifest_path = source / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    rows, changes, receipts = [], [], {}
    for path in sorted(source.glob("*/result.json")):
        row = json.loads(path.read_text())
        world_path = path.with_name("world.json")
        state = json.loads(world_path.read_text())
        previous = row["score"]
        error = previous["error"]
        if error == "Frozen API does not cover all requested endpoints":
            error = None
        revised = score(
            state,
            finished=previous["finished"],
            final_response=row["final_response"],
            error=error,
        )
        rows.append({**row, "score": revised})
        if revised != previous:
            changes.append(
                {"trial": path.parent.name, "before": previous, "after": revised}
            )
        for artifact in (path, world_path):
            receipts[str(artifact.relative_to(source))] = hashlib.sha256(
                artifact.read_bytes()
            ).hexdigest()
    cases = []
    for case in manifest["cases"]:
        target = scenario(case["id"]).get("target_violation")
        cases.append(case | ({"target_violation": target} if target else {}))
    report = compare(rows, cases, manifest["repeats"])
    if not report["complete"]:
        raise ValueError("Only rescore a complete comparison")
    output.mkdir(parents=True, exist_ok=False)
    write(
        output / "manifest.json",
        {
            **manifest,
            "cases": cases,
            "rescoring": {
                "source_manifest_sha256": hashlib.sha256(
                    manifest_path.read_bytes()
                ).hexdigest(),
                "scorer_sha256": hashlib.sha256(
                    Path(__file__).with_name("reviewer_world.py").read_bytes()
                ).hexdigest(),
                "comparison_scorer_sha256": hashlib.sha256(
                    Path(__file__).with_name("compare.py").read_bytes()
                ).hexdigest(),
                "scoring_sha256": {
                    name: hashlib.sha256(
                        Path(__file__).with_name(name).read_bytes()
                    ).hexdigest()
                    for name in (
                        "compare.py",
                        "reviewer_world.py",
                        "triage_world.py",
                        "scenarios.py",
                        "rescore.py",
                    )
                },
                "source_receipts": receipts,
                "reason": "Recompute task scores and comparison acceptance from retained executions",
                "agent_reexecutions": 0,
            },
        },
    )
    write(output / "comparison.json", report)
    write(output / "changes.json", changes)
    return {
        "status": report["status"],
        "trials": len(rows),
        "changed_scores": len(changes),
        "agent_reexecutions": 0,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    print(json.dumps(rescore(args.source, args.output)))
