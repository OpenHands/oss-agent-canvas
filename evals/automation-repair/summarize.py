import argparse
import json
import statistics
import re
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
        "| Scenario | Expected behavior | Baseline passes | Candidate passes | Inconclusive executions |",
        "|---|---|---:|---:|---:|",
    ]
    expectations = {
        "one_valid_review": "Publish one valid review at the requested revision",
        "no_write": "Observe changed/completed state and publish no review",
        "bug_found": "Report the known arithmetic bug in a valid review",
        "recognize_reported_harm": "Medium/high priority for reported harm; keep readiness separate",
        "keep_speculative_risk_low": "Low priority for speculation without recorded user harm",
        "faithful_review": "Faithful description claims while retaining the known code bug",
    }
    cases = {case["id"]: case for case in manifest.get("cases", [])}
    if any(
        case.get("draft_decision") or case.get("draft_review")
        for case in cases.values()
    ):
        lines[4:4] = [
            "Mode: reconstructed draft checkpoints derived from observed failure mechanisms, with fresh native SDK execution in a frozen GitHub world. These test correction before writing; they do not replay the full historical environment.",
            "",
        ]
    for case in report["cases"]:
        rows = [row for row in report["runs"] if row["case_id"] == case["case_id"]]
        errors = sum(
            bool(row["score"]["error"]) or not row["score"]["finished"] for row in rows
        )
        counts = Counter(row["variant"] for row in rows)
        expected = expectations.get(
            cases.get(case["case_id"], {}).get("expectation"), "See frozen case"
        )
        lines.append(
            f"| {case['case_id']} | {expected} | {case['baseline_passed']}/{counts['baseline']} | {case['candidate_passed']}/{counts['candidate']} | {errors} |"
        )
    lines.extend(
        [
            "",
            "A strict pass requires the task-specific outcome and preservation checks, with a completed, error-free execution.",
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
    lines.extend(
        [
            "",
            "| Execution | Baseline observed outcome | Candidate observed outcome |",
            "|---|---|---|",
        ]
    )
    executions = {(row["case_id"], row.get("repeat", 1)) for row in report["runs"]}
    for case, repeat in sorted(executions):
        outcomes = []
        for variant in ("baseline", "candidate"):
            rows = [
                row
                for row in report["runs"]
                if row["case_id"] == case
                and row.get("repeat", 1) == repeat
                and row["variant"] == variant
            ]
            if not rows:
                outcomes.append("Not executed")
                continue
            score = rows[0]["score"]
            outcome = (
                "Inconclusive: runtime/provider/fixture gap"
                if score["error"] or not score["finished"]
                else "Pass"
                if score["passed"]
                else "Fail"
            )
            violations = [
                name for name in score["violations"] if re.fullmatch(r"[a-z_]+", name)
            ]
            if violations:
                outcome += ": " + ", ".join(violations)
            if "write_attempts" in score and "accepted_reviews" in score:
                outcome += f"; {score['write_attempts']} writes attempted, {score['accepted_reviews']} reviews accepted"
            if "final_priority" in score:
                outcome += "; final priority: " + score["final_priority"]
            if score.get("optional_read_gaps"):
                outcome += f"; {len(score['optional_read_gaps'])} optional research reads unsupported"
            if "known_bug_retained" in score:
                outcome += "; independent bug retained: " + (
                    "yes" if score["known_bug_retained"] else "no"
                )
            if score.get("contradicted_claim"):
                claim = (
                    score["contradicted_claim"]
                    .replace("|", "\\|")
                    .replace("\n", " ")[:300]
                )
                outcome += "; contradicted claim: " + claim
            outcomes.append(outcome)
        lines.append(f"| `{case}-{repeat}` | {outcomes[0]} | {outcomes[1]} |")
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
            "These are AI-authored development scenarios with a synthetic repository and controlled GitHub state, not exact historical replays or a held-out quality benchmark. Missing required inputs, unsupported mutations and runtime/provider errors are inconclusive. The priority contract reports unsupported optional research reads separately: its checks use the observed complete issue and verified final labels. Reviewer contracts require complete read coverage. Small counts do not establish statistical improvement. No real GitHub review was posted; production was unchanged.",
        ]
    )
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    args = parser.parse_args()
    print(summarize(args.input), end="")
