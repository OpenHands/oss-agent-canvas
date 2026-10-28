import math
import time


class BudgetExhausted(RuntimeError):
    pass


class RunBudget:
    def __init__(self, config):
        self.maximum_cost = config.get("max_inference_cost_usd", 1.0)
        self.maximum_seconds = config.get("max_run_seconds", 900)
        for name, value in (
            ("max_inference_cost_usd", self.maximum_cost),
            ("max_run_seconds", self.maximum_seconds),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(name + " must be a positive finite number")
        self.started = time.monotonic()
        self.cost, self.requests, self.unknown = 0.0, 0, 0
        self.reason = None

    def check(self):
        if self.unknown:
            self.reason = "inference_cost_unknown"
        elif self.cost >= self.maximum_cost:
            self.reason = "inference_cost_threshold"
        elif time.monotonic() - self.started >= self.maximum_seconds:
            self.reason = "run_time_threshold"
        if self.reason:
            raise BudgetExhausted(self.reason)

    def record(self, value):
        self.requests += 1
        try:
            cost = float(value)
            if not math.isfinite(cost) or cost < 0:
                raise ValueError("Invalid inference cost")
            self.cost += cost
        except (TypeError, ValueError):
            self.unknown += 1

    def snapshot(self):
        return {
            "max_inference_cost_usd": self.maximum_cost,
            "reported_inference_cost_usd": self.cost,
            "requests": self.requests,
            "unknown_cost_requests": self.unknown,
            "max_run_seconds": self.maximum_seconds,
            "elapsed_seconds": round(time.monotonic() - self.started, 3),
            "stop_reason": self.reason,
        }
