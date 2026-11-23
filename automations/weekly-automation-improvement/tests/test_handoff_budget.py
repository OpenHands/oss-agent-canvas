import pytest

import budget as module
from budget import BudgetExhausted, RunBudget


def test_handoff_accounts_for_time_between_workflows(monkeypatch):
    monkeypatch.setattr(module.time, "monotonic", lambda: 100)
    monkeypatch.setattr(module.time, "time", lambda: 200)
    original = RunBudget({"max_run_seconds": 10})
    original.record(0.1)
    snapshot = original.snapshot() | {"elapsed_seconds": 2}
    resumed = RunBudget.restore(snapshot, 195)
    assert resumed.timeout(20) == 3
    assert resumed.cost == 0.1 and resumed.requests == 1
    monkeypatch.setattr(module.time, "time", lambda: 204)
    with pytest.raises(BudgetExhausted, match="run_time_threshold"):
        RunBudget.restore(snapshot, 195).check()


def test_handoff_retains_unknown_inference_cost():
    original = RunBudget({})
    original.record(None)
    with pytest.raises(BudgetExhausted, match="inference_cost_unknown"):
        RunBudget.restore(original.snapshot(), module.time.time()).check()
