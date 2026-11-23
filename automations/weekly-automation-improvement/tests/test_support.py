import json
import sys
import uuid
from pathlib import Path
from types import ModuleType, SimpleNamespace


def arguments(tmp_path, **overrides):
    values = dict(
        output=tmp_path,
        project_id=str(uuid.UUID(int=1)),
        app_url="https://example.test",
        since="2026-10-10T00:00:00Z",
        until="2026-10-17T00:00:00Z",
        population_limit=100,
        seed=42,
        limit=3,
        max_spans=1000,
        max_sample_bytes=2000000,
        max_response_bytes=8000000,
        input=None,
        source_config=None,
        collect_only=False,
        model="small",
        coordinator="large",
        api="responses",
        effort="low",
        max_input_bytes=750000,
        max_output_tokens=4096,
        evaluate_fix=False,
        publish_fix=False,
    )
    return SimpleNamespace(**(values | overrides))


def sample():
    return {
        "case_id": "c1",
        "conversation_id": str(uuid.UUID(int=2)),
        "automation_id": "automation",
        "run_id": "run",
        "automation_name": "Example",
        "trace_ids": [str(uuid.UUID(int=3))],
        "initial_instructions": [
            {"role": "user", "content": "Report test failures honestly"}
        ],
        "spans": [
            {
                "ref": "s1",
                "span_id": "00000000-0000-0000-8000-000000123456",
                "parent_span_id": None,
                "trace_id": str(uuid.UUID(int=3)),
                "name": "terminal",
                "span_type": "TOOL",
                "status": "success",
                "input": "pytest -q",
                "output": "FAILED: expected 4, got 3; exit code 1",
                "content_clipped": False,
            }
        ],
        "signals": [],
    }


def snapshot():
    return {
        "schema_version": 1,
        "project_id": str(uuid.UUID(int=1)),
        "app_url": "https://example.test",
        "selection": {"population_sha256": "a" * 64},
        "samples": [sample()],
    }


def candidate():
    return {
        "summary": "Failure hidden",
        "signature": "test-result-reporting",
        "cause": "workflow",
        "outcome": "unresolved",
        "evidence": ["s1"],
        "recommendation": "Report the failure",
    }


SIGNAL = str(uuid.UUID(int=10))
RUN = str(uuid.UUID(int=11))
EVENT = str(uuid.UUID(int=12))


def frozen(with_finding=True):
    value = snapshot()
    value["analysis_backend"] = "flow1"
    value["flow1_signal"] = {
        "id": SIGNAL,
        "projectId": value["project_id"],
        "model": "flow-1",
        "llmProfileId": str(uuid.UUID(int=13)),
        "version": 1,
    }
    value["flow1_profile"] = {
        "id": value["flow1_signal"]["llmProfileId"],
        "provider": "custom",
        "config": {"baseUrl": "https://api.lmnr.ai/v1"},
        "models": ["flow-1"],
    }
    case = value["samples"][0]
    case["flow1_trace_sessions"] = [
        {
            "trace_id": case["trace_ids"][0],
            "conversation_ids": [case["conversation_id"]],
        }
    ]
    case["flow1_runs"] = [
        {
            "signal_id": SIGNAL,
            "signal_version": 1,
            "run_id": RUN,
            "trace_id": case["trace_ids"][0],
            "status": "COMPLETED",
            "updated_at": "2026-10-22T00:00:00Z",
            "event_id": EVENT if with_finding else str(uuid.UUID(int=0)),
            "input_tokens": 100,
            "cache_read_tokens": 0,
            "output_tokens": 20,
        }
    ]
    payload = candidate() | {
        "evidence": f"[failure](https://laminar.app.all-hands.dev/project/{value['project_id']}/traces/{case['trace_ids'][0]}?spanId={case['spans'][0]['span_id']}&chat=true)"
    }
    case["flow1_events"] = (
        [
            {
                "id": EVENT,
                "signal_id": SIGNAL,
                "signal_version": 1,
                "run_id": RUN,
                "trace_id": case["trace_ids"][0],
                "payload": json.dumps(payload),
            }
        ]
        if with_finding
        else []
    )
    return value


def install_sdk(monkeypatch, execute):
    captured = {"instances": []}
    agent = SimpleNamespace(
        tools=[
            SimpleNamespace(name=name)
            for name in ("terminal", "file_editor", "browser", "task")
        ]
    )

    class Conversation:
        def __init__(self, **kwargs):
            assert "max_budget_per_run" not in kwargs
            captured["conversation"] = kwargs
            captured["instance"] = self
            captured["instances"].append(self)
            self.kwargs, self.messages = kwargs, []
            self.cost, self.closed = 0.0, False
            self.state = SimpleNamespace(
                execution_status=SimpleNamespace(value="finished")
            )
            self.id = "native-sdk-conversation-" + str(len(captured["instances"]))
            self.conversation_stats = SimpleNamespace(
                get_combined_metrics=lambda: SimpleNamespace(accumulated_cost=self.cost)
            )

        def send_message(self, prompt):
            self.messages.append(prompt)
            captured["prompt"] = prompt

        def run(self):
            captured.update(
                instance=self, conversation=self.kwargs, prompt=self.messages[-1]
            )
            self.cost += 0.12
            execute(Path(captured["conversation"]["workspace"]))

        def close(self):
            self.closed = True
            captured["closed"] = True

    modules = {
        "openhands": {},
        "openhands.sdk": {
            "Conversation": Conversation,
            "LLM": lambda **kwargs: captured.update(llm=kwargs) or object(),
        },
        "openhands.sdk.tool": {"Tool": lambda name: SimpleNamespace(name=name)},
        "openhands.tools": {},
        "openhands.tools.preset": {},
        "openhands.tools.preset.default": {
            "get_default_agent": lambda **kwargs: agent,
            "register_builtins_agents": lambda: None,
        },
        "openhands.tools.task": {"TaskToolSet": SimpleNamespace(name="task")},
    }
    for name, values in modules.items():
        module = ModuleType(name)
        module.__dict__.update(values)
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setenv("LLM_BASE_URL", "https://example.test")
    monkeypatch.setenv("LITELLM_API_KEY", "private-test-key")
    return captured
