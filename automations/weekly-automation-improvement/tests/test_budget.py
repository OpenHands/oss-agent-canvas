from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "tarball"))
from budget import BudgetExhausted, RunBudget


def test_cost_threshold_stops_next_request_without_pr_quota():
    budget = RunBudget({"max_inference_cost_usd": 0.03})
    for _ in range(6):
        budget.check()
        budget.record("0.0051")
    with pytest.raises(BudgetExhausted, match="inference_cost_threshold"):
        budget.check()
    assert budget.snapshot()["requests"] == 6


def test_unknown_or_invalid_cost_stops_further_inference():
    for value in (None, "not-a-cost", -1, "nan", "inf"):
        budget = RunBudget({})
        budget.record(value)
        with pytest.raises(BudgetExhausted, match="inference_cost_unknown"):
            budget.check()


def test_time_threshold_uses_monotonic_clock(monkeypatch):
    import budget as module

    clock = [10]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    budget = RunBudget({"max_run_seconds": 5})
    budget.check()
    clock[0] = 16
    with pytest.raises(BudgetExhausted, match="run_time_threshold"):
        budget.check()


def test_expired_deadline_prevents_tests_and_stops_the_remaining_commands(
    tmp_path, monkeypatch
):
    import budget as module
    from patches import run_checks

    clock = [10]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    budget = RunBudget({"max_run_seconds": 5})
    clock[0] = 16
    with pytest.raises(BudgetExhausted):
        run_checks(
            tmp_path,
            [
                {
                    "name": "blocked",
                    "argv": [sys.executable, "-c", "raise Exception('must not start')"],
                }
            ],
            tmp_path,
            "baseline",
            budget=budget,
        )
    assert not list(tmp_path.glob("*.log"))


def test_check_timeout_kills_descendants_and_retains_partial_evidence(tmp_path):
    import json
    from patches import run_checks

    budget = RunBudget({"max_run_seconds": 0.3})
    marker = tmp_path / "child.txt"
    child = (
        f"import time,pathlib; time.sleep(0.8); pathlib.Path({str(marker)!r}).touch()"
    )
    checks = [
        {
            "name": "slow",
            "argv": [
                sys.executable,
                "-c",
                f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{child!r}]); time.sleep(10)",
            ],
        },
        {
            "name": "never",
            "argv": [sys.executable, "-c", "raise Exception('must not start')"],
        },
    ]
    with pytest.raises(BudgetExhausted):
        run_checks(tmp_path, checks, tmp_path, "baseline", budget=budget)
    receipt = json.loads((tmp_path / "baseline-0-check.json").read_text())
    assert receipt["exit_code"] == 124
    import time

    time.sleep(1)
    assert not marker.exists() and not (tmp_path / "baseline-1.log").exists()


def test_real_model_wrapper_accounts_cost_and_blocks_next_call(tmp_path, monkeypatch):
    import io
    import json
    import common
    from test_automation import arguments, app

    monkeypatch.setenv("LITELLM_API_KEY", "budget-test-credential")
    monkeypatch.setenv("LLM_BASE_URL", "https://proxy.example.test")
    requests = []

    def respond(request, timeout):
        requests.append(request)
        response = io.BytesIO(
            json.dumps(
                {
                    "output": [
                        {
                            "type": "function_call",
                            "name": "report",
                            "arguments": '{"findings":[]}',
                        }
                    ]
                }
            ).encode()
        )
        response.headers = {"x-litellm-response-cost": "0.6"}
        return response

    monkeypatch.setattr(common.urllib.request, "urlopen", respond)
    budget = RunBudget({"max_inference_cost_usd": 1})
    for i in range(2):
        common.call_model(
            arguments(tmp_path / str(i), budget=budget),
            "model",
            "Inspect",
            {},
            "one",
            app.ANALYSIS,
        )
    with pytest.raises(BudgetExhausted):
        common.call_model(
            arguments(tmp_path / "blocked", budget=budget),
            "model",
            "Inspect",
            {},
            "one",
            app.ANALYSIS,
        )
    assert len(requests) == 2 and budget.snapshot()[
        "reported_inference_cost_usd"
    ] == pytest.approx(1.2)
    assert not (tmp_path / "blocked").exists()
