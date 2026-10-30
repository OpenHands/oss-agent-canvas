def assess(report, cases, repeats=1, semantic=None, unchanged=False):
    indexed = {}
    duplicates = []
    for row in report["runs"]:
        key = (row["case_id"], row.get("repeat", 1), row["variant"])
        if key in indexed:
            duplicates.append(key)
        indexed[key] = row
    reviews = {
        (item["case_id"], item.get("repeat", 1)): item for item in semantic or []
    }
    observations, fixes, regressions, gaps, missing = [], [], [], [], []
    historical = any(case.get("semantic_required") for case in cases)
    controls = []
    reproductions = []
    for case in cases:
        for repeat in range(1, repeats + 1):
            base = indexed.get((case["id"], repeat, "baseline"))
            candidate = indexed.get((case["id"], repeat, "candidate"))
            identity = {"case_id": case["id"], "repeat": repeat}
            review = reviews.get((case["id"], repeat))
            context_valid = not historical or (
                case.get("repository_snapshot", {}).get("archive_path")
                and review
                and review.get("context_sufficient") is True
            )
            baseline_valid = bool(
                context_valid
                and base
                and base["score"]["finished"]
                and not base["score"]["error"]
                and not base["score"].get("invalid_replay_responses")
            )
            target = case.get("target_violation")
            reproduced = None
            if baseline_valid:
                if review:
                    reproduced = review["target_reproduced"]
                elif target:
                    reproduced = target in base["score"]["violations"]
            if case["kind"] == "regression":
                reproductions.append(reproduced)
            if not base or not candidate:
                missing.append(identity)
                continue
            valid = bool(
                baseline_valid
                and candidate["score"]["finished"]
                and not candidate["score"]["error"]
                and not candidate["score"].get("invalid_replay_responses")
            )
            if not valid:
                gaps.append(identity)
            fixed = (
                bool(reproduced and target not in candidate["score"]["violations"])
                if target
                else None
            )
            if review:
                fixed = (
                    review["target_fixed"]
                    and review["baseline_success"] is False
                    and review["candidate_success"] is True
                )
            new_violations = sorted(
                set(candidate["score"]["violations"]) - set(base["score"]["violations"])
            )
            regression = valid and (
                (base["score"]["passed"] and not candidate["score"]["passed"])
                or bool(new_violations)
                or bool(
                    review
                    and (
                        review["regression"] is True
                        or (
                            review["baseline_success"] is True
                            and review["candidate_success"] is False
                        )
                    )
                )
            )
            observation = identity | {
                "valid": valid,
                "baseline_valid": baseline_valid,
                "baseline_conformance": base["score"]["passed"],
                "candidate_conformance": candidate["score"]["passed"],
                "target_reproduced": reproduced,
                "target_fixed": fixed if valid else None,
                "new_violations": new_violations if valid else [],
                "semantic": review,
            }
            observations.append(observation)
            if (
                valid
                and reproduced
                and fixed
                and case["kind"] == "regression"
                and (not review or review.get("executed_change", True))
            ):
                fixes.append(identity)
            if regression:
                regressions.append(identity)
            if case["kind"] == "control":
                controls.append(observation)
    complete = not missing and not duplicates
    validity = complete and not gaps
    candidate_conformance = bool(observations) and all(
        item["candidate_conformance"] for item in observations
    )
    control_coverage = bool(controls) and all(item["valid"] for item in controls)
    negative_success = not historical or all(
        item["semantic"] is not None and item["semantic"]["candidate_success"] is True
        for item in observations
        if next(case["kind"] for case in cases if case["id"] == item["case_id"])
        == "regression"
    )
    semantic_complete = not historical or all(
        item["semantic"] is not None
        and all(
            item["semantic"][key] is not None
            for key in ("baseline_success", "candidate_success", "regression")
        )
        for item in observations
    )
    supported = (
        validity
        and control_coverage
        and candidate_conformance
        and semantic_complete
        and negative_success
        and not regressions
        and len(fixes) >= (2 if historical else 1)
        and not unchanged
    )
    status = (
        "regressed"
        if regressions
        else "improvement_on_development_cases"
        if supported
        else "inconclusive"
    )
    return {
        "status": status,
        "mechanism_supported": bool(fixes) and not historical and not unchanged,
        "target_reproduced": True
        if any(value is True for value in reproductions)
        else None
        if any(value is None for value in reproductions) or not reproductions
        else False,
        "target_fixed_pairs": fixes,
        "observed_regressions": regressions,
        "experiment_valid": validity,
        "complete": complete,
        "missing_pairs": missing,
        "invalid_pairs": gaps,
        "duplicate_runs": duplicates,
        "control_coverage": {
            "planned_inputs": sum(case["kind"] == "control" for case in cases),
            "valid_pairs": sum(item["valid"] for item in controls),
            "planned_pairs": repeats * sum(case["kind"] == "control" for case in cases),
        },
        "semantic_review_complete": semantic_complete,
        "observations": observations,
        "observed_deliverable_failures": [
            {
                "case_id": row["case_id"],
                "repeat": row.get("repeat", 1),
                "variant": row["variant"],
                "violations": row["score"]["violations"],
            }
            for row in report["runs"]
            if row["score"]["finished"]
            and row["score"].get("deliverable_complete") is False
        ],
        "claim": "paired_development_improvement"
        if supported
        else "observed_regression"
        if regressions
        else "mechanism_tested"
        if fixes and not historical and not unchanged
        else "repair_unverified",
        "statistical_improvement_proven": False,
        "unchanged_source_control": unchanged,
    }
