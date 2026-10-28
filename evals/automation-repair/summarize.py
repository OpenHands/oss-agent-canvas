import argparse
import json
import statistics
from collections import Counter
from pathlib import Path


def summarize(root):
    report = json.loads((root / "comparison.json").read_text())
    manifest = json.loads((root / "manifest.json").read_text())
    lines = [
        f"Behavioral comparison: **{report['status']}**.",
        "",
        f"Baseline `{manifest['baseline'][:12]}`; candidate `{manifest['candidate'][:12]}`. "
        f"Model `{manifest['model']}`, effort `{manifest['effort']}`. "
        f"{len(report['runs'])} recorded executions; {manifest['repeats']} planned repetitions per version/case.",
        "",
        "| Scenario | Baseline passes | Candidate passes | Inconclusive executions |",
        "|---|---:|---:|---:|",
    ]
    for case in report["cases"]:
        rows = [row for row in report["runs"] if row["case_id"] == case["case_id"]]
        errors = sum(
            bool(row["score"]["error"]) or not row["score"]["finished"] for row in rows
        )
        counts = Counter(row["variant"] for row in rows)
        lines.append(
            f"| {case['case_id']} | {case['baseline_passed']}/{counts['baseline']} | {case['candidate_passed']}/{counts['candidate']} | {errors} |"
        )
    lines.extend(
        [
            "",
            "A strict pass includes the publication contract and final response; finding a bug alone does not pass.",
            "",
            "| Recorded violation | Baseline runs | Candidate runs |",
            "|---|---:|---:|",
        ]
    )
    violations = {
        variant: Counter(
            violation
            for row in report["runs"]
            if row["variant"] == variant
            for violation in set(row["score"]["violations"])
        )
        for variant in ("baseline", "candidate")
    }
    for name in sorted(set(violations["baseline"]) | set(violations["candidate"])):
        lines.append(
            f"| {name} | {violations['baseline'][name]} | {violations['candidate'][name]} |"
        )
    lines.append("")
    for variant in ("baseline", "candidate"):
        measurements = [
            row["measurements"] for row in report["runs"] if row["variant"] == variant
        ]
        seconds = [row["seconds"] for row in measurements]
        usages = [
            row.get("sdk_metrics", {}).get("accumulated_token_usage") or {}
            for row in measurements
        ]
        cost = sum(
            row.get("sdk_metrics", {}).get("accumulated_cost", 0)
            for row in measurements
        )
        latency = (
            f"{statistics.median(seconds):.2f}s median execution time"
            if seconds
            else "no timing data"
        )
        cost_text = (
            f"${cost:.4f} SDK-estimated cost (not billed cost)"
            if cost > 0
            else "cost unknown (zero SDK estimates are not proof of free inference)"
        )
        lines.append(
            f"{variant.capitalize()}: {latency}; {sum(u.get('prompt_tokens', 0) for u in usages):,} input / {sum(u.get('completion_tokens', 0) for u in usages):,} output tokens; {cost_text}."
        )
    lines.extend(
        [
            "",
            "These are AI-authored development scenarios with a synthetic repository and controlled GitHub state, not exact historical replays or a held-out quality benchmark. Unsupported API/Git reads and runtime/provider errors are inconclusive. Small counts do not establish statistical improvement. No real GitHub review was posted; production was unchanged.",
        ]
    )
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    args = parser.parse_args()
    print(summarize(args.input), end="")
