import argparse
import hashlib
import json
import os
import re
from pathlib import Path
from types import SimpleNamespace

import compare
from scenarios import scenario
from contracts import captured_inputs, select
from summarize import summarize
from rescore import rescore

SOURCE = "plugins/github-pr-reviewer-events/"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def settings(request, output):
    plan = request["plan"]
    topic = (request.get("topic") or {}).get("key")
    topics = plan.get("topics", {})
    targeted = topic in topics
    cases = topics[topic]["cases"] if targeted else ["healthy", "buggy"]
    if plan.get("automatic_contracts", False):
        _, cases = select(request)
        if not cases:
            raise ValueError("No validated execution contract covers this repair")
        targeted = True
    if (
        not isinstance(cases, list)
        or not cases
        or len(cases) != len(set(cases))
        or not all(isinstance(name, str) for name in cases)
    ):
        raise ValueError("Configure distinct frozen reviewer cases")
    kinds = {scenario(name)["kind"] for name in cases}
    if "control" not in kinds or (targeted and "regression" not in kinds):
        raise ValueError("A topic comparison needs a regression case and a control")
    for name, low, high, default in (
        ("repeats", 1, 5, 1),
        ("max_steps", 1, 60, 30),
        ("max_run_seconds", 30, 1200, 180),
        ("llm_timeout", 30, 180, 120),
    ):
        value = plan.get(name, default)
        if type(value) is not int or not low <= value <= high:
            raise ValueError("Invalid comparison " + name)
    for name in ("baseline", "candidate"):
        if not re.fullmatch(r"[0-9a-f]{40}", request[name]):
            raise ValueError("Pin full baseline and candidate commit SHAs")
    return SimpleNamespace(
        checkout=Path(request["checkout"]).expanduser().resolve(),
        output=output,
        baseline=request["baseline"],
        candidate=request["candidate"],
        model=plan["model"],
        base_url=plan["base_url"],
        effort=plan.get("effort", "low"),
        image=plan.get("image", compare.IMAGE),
        repeats=plan.get("repeats", 1),
        max_steps=plan.get("max_steps", 30),
        max_run_seconds=plan.get("max_run_seconds", 180),
        llm_timeout=plan.get("llm_timeout", 120),
        cases=",".join(cases),
        resume=False,
        case_file=Path(plan["case_file"]) if plan.get("case_file") else None,
    ), targeted


def run(request_path, output):
    os.umask(0o077)
    request = json.loads(request_path.read_text())
    output.mkdir(parents=True)
    receipt = {
        "baseline": request["baseline"],
        "candidate": request["candidate"],
        "status": "inconclusive",
        "historical_replay": False,
        "request_sha256": digest(request_path),
    }
    changed = request["changed_files"]
    contract, _ = select(request)
    if (
        not changed
        or (request["plan"].get("automatic_contracts", False) and not contract)
        or any(
            not path.startswith((SOURCE, "plugins/issue-triage/")) for path in changed
        )
    ):
        receipt["markdown"] = (
            "**Not evaluated:** no trusted execution contract covers this repair. "
            "No behavioral improvement is demonstrated for this repair."
        )
    else:
        args, targeted = settings(request, output / "runs")
        if request.get("snapshot"):
            inputs = captured_inputs(
                json.loads(Path(request["snapshot"]).read_text()), contract
            )
            if inputs:
                args.case_file = output / "captured-inputs.json"
                compare.write(args.case_file, inputs)
        recorded = request["plan"].get("recorded_runs")
        if recorded:
            original = Path(recorded).resolve()
            manifest = json.loads((original / "manifest.json").read_text())
            for name in (
                "baseline",
                "candidate",
                "model",
                "effort",
                "repeats",
                "max_steps",
                "max_run_seconds",
            ):
                if manifest[name] != getattr(args, name):
                    raise ValueError(
                        "Recorded executions differ from requested " + name
                    )
            if manifest["llm_base_url"] != args.base_url:
                raise ValueError("Recorded executions used a different model endpoint")
            if manifest.get("llm_timeout", 60) != args.llm_timeout:
                raise ValueError("Recorded executions used a different request timeout")
            if [case["id"] for case in manifest["cases"]] != args.cases.split(","):
                raise ValueError("Recorded executions use different cases")
            inputs = json.loads(args.case_file.read_text()) if args.case_file else {}
            for case in manifest["cases"]:
                expected = scenario(case["id"]) | inputs.get(case["id"], {})
                if any(
                    case.get(key) != value
                    for key, value in expected.items()
                    if key != "target_violation"
                ):
                    raise ValueError("Recorded inputs or acceptance checks differ")
            rescore(original, args.output)
        else:
            compare.main(args)
        report = json.loads((args.output / "comparison.json").read_text())
        receipt.update(
            status=report["status"],
            target_topic_exercised=targeted,
            contract=contract,
            reused_executions=bool(recorded),
            manifest_sha256=digest(args.output / "manifest.json"),
            comparison_sha256=digest(args.output / "comparison.json"),
        )
        if not targeted and receipt["status"] != "regressed":
            receipt["status"] = "inconclusive"
        receipt["markdown"] = (
            f"**Verdict: {receipt['status']}**. "
            + (
                "The configured failure scenario and controls were executed on both versions.\n\n"
                if targeted
                else "No frozen regression scenario covers this repair topic. Only reviewer controls were run; the intended improvement remains unproven.\n\n"
            )
            + f"Baseline: `{request['baseline']}`. Repaired: `{request['candidate']}`.\n\n"
            + summarize(args.output)
            + (
                "\nThis candidate was revised using earlier failed development-case feedback. These are development results, not independent held-out validation.\n"
                if request.get("development_feedback_used")
                else ""
            )
            + "\n### Evidence provenance\n\n"
            + (
                "Reused recorded executions and rescored every trial; no agent run was repeated. Original artifacts remain retained.\n\n"
                if recorded
                else ""
            )
            + f"Manifest SHA-256 (concatenate): `{receipt['manifest_sha256'][:32]}` `{receipt['manifest_sha256'][32:]}`.\n\n"
            + f"Recorded comparison SHA-256 (concatenate): `{receipt['comparison_sha256'][:32]}` `{receipt['comparison_sha256'][32:]}`.\n\n"
            + "Full prompts, tool/API logs and run results are retained privately with the comparison request. "
            "The table reports observed outcomes on frozen development cases, not proof of general task quality. "
            "Both versions were run afresh; historical production logs were not replayed.\n"
        )
    compare.write(output / "evidence.json", receipt)
    return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = run(args.request.resolve(), args.output.resolve())
    print(json.dumps({"status": result["status"]}))
