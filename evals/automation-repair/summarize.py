import argparse
import json
import statistics
import re
from collections import Counter
from pathlib import Path


def cohort_sections(report, cases):
    size = next(iter(cases.values()))["historical_sample_size"]
    lines = [
        f"Sampling parameter **N={size}**. Each selected input is executed on both baseline and candidate. Non-negative inputs had completed Flow-1 analysis with no finding; they are not assumed healthy.",
        "",
    ]
    for kind, heading in (
        ("regression", "Negative Signal inputs"),
        ("control", "Random non-negative inputs"),
    ):
        selected = [case for case in cases.values() if case["kind"] == kind]
        lines.extend(
            [
                f"### {heading} ({len(selected)} inputs, N={size})",
                "",
                "| Input | Before conformance | After conformance | Assessment |",
                "|---|---:|---:|---|",
            ]
        )
        for case in selected:
            rows = [row for row in report["runs"] if row["case_id"] == case["id"]]
            baseline = [row for row in rows if row["variant"] == "baseline"]
            candidate = [row for row in rows if row["variant"] == "candidate"]
            passed = bool(candidate) and all(
                row["score"]["passed"] for row in candidate
            )
            worsened = any(
                row.get("introduced_violations", row["score"]["violations"])
                for row in candidate
            )
            reproduced = any(
                case.get("target_violation") in row["score"]["violations"]
                for row in baseline
            )
            error = any(
                row["score"]["error"] or not row["score"]["finished"] for row in rows
            )
            observed = [
                item
                for item in report.get("assessment", {}).get("observations", [])
                if item["case_id"] == case["id"]
            ]
            judgments = [item["semantic"] for item in observed if item.get("semantic")]
            if judgments:
                outcome = " / ".join(
                    dict.fromkeys(
                        item["reason"].replace("|", "\\|").replace("\n", " ")
                        for item in judgments
                    )
                )
            else:
                outcome = (
                    "Not established; semantic review pending"
                    if case.get("semantic_required")
                    else "Inconclusive"
                    if error or len(baseline) != len(candidate) or not candidate
                    else "Regression / failed check"
                    if not passed and (kind == "regression" or worsened)
                    else "No new violations; existing failures remain"
                    if not passed
                    else "Fixed reproduced target"
                    if kind == "regression" and reproduced
                    else "Not reproduced"
                    if kind == "regression"
                    else "Passed regression checks"
                )
            lines.append(
                f"| [{case['id']}]({case['trace_url']}) | {sum(row['score']['passed'] for row in baseline)}/{len(baseline)} | {sum(row['score']['passed'] for row in candidate)}/{len(candidate)} | {outcome} |"
            )
        lines.append("")
    return lines


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
    assessment = report.get("assessment", {})
    if assessment:
        lines[4:4] = [
            "| Evidence dimension | Result |",
            "|---|---|",
            f"| Claim supported | {assessment['claim']} |",
            f"| Target mechanism supported | {assessment['mechanism_supported']} |",
            f"| Experiment fully valid | {assessment['experiment_valid']} |",
            f"| Target reproduced | {assessment['target_reproduced'] if assessment['target_reproduced'] is not None else 'Unknown'} |",
            f"| Paired target fixes | {len(assessment['target_fixed_pairs'])} |",
            f"| Observed regression pairs | {len(assessment['observed_regressions'])} |",
            f"| Finished runs missing a deliverable | {len(assessment.get('observed_deliverable_failures', []))} |",
            f"| Valid control pairs | {assessment['control_coverage']['valid_pairs']}/{assessment['control_coverage']['planned_pairs']} |",
            "",
            "Positive mechanism tests and observed regressions remain visible when other trials lack coverage. A mechanism test does not establish general task quality; small stochastic comparisons do not establish statistical improvement.",
            "",
        ]
    if report.get("semantic_review_error"):
        lines[4:4] = [
            "Semantic review could not complete: "
            + report["semantic_review_error"].replace("\n", " "),
            "",
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
    historical = any(case.get("native_payload") for case in cases.values())
    if historical:
        lines = lines[:4] + lines[4:-2] + cohort_sections(report, cases)
    if any(
        case.get("draft_decision") or case.get("draft_review")
        for case in cases.values()
    ):
        lines[4:4] = [
            "Mode: reconstructed draft checkpoints derived from observed failure mechanisms, with fresh native SDK execution in a frozen GitHub world. These test correction before writing; they do not replay the full historical environment.",
            "",
        ]
    for case in [] if historical else report["cases"]:
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
            "Controls must introduce no new violations relative to their paired baseline; existing failures remain visible. Required-input and execution errors on either version block acceptance."
            if historical
            else "Candidate controls must pass. Completed baseline behavioral failures remain visible; required-input and execution errors on either version block acceptance.",
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
            if "final_priority" in score:
                outcome += f"; {score['write_attempts']} writes attempted"
            elif "write_attempts" in score and "accepted_reviews" in score:
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
            (
                "These are actual historical task inputs with task-level judgments from one validation agent inspecting paired recorded executions. Both source versions execute in a controlled GitHub world with pinned current repository guidance. Historical repository state, all implementation files and accumulated conversation history are not fully reconstructed; this checks the priority/readiness/preservation contract, not general task quality."
                if historical
                else "These are AI-authored development scenarios with a synthetic repository and controlled GitHub state, not exact historical replays or a held-out quality benchmark."
            )
            + " Missing required inputs, unsupported mutations and runtime/provider errors are inconclusive. Required API/context gaps invalidate the affected pair. Rejected API writes and incomplete deliverables are reported separately. Reviewer contracts require complete read coverage. Small counts do not establish statistical improvement. No real GitHub review was posted; production was unchanged.",
        ]
    )
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    args = parser.parse_args()
    print(summarize(args.input), end="")
