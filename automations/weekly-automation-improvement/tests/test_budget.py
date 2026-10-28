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
