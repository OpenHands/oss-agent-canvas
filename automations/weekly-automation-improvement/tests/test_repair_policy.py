import pytest

from repair_policy import instructions, permits, resolve, resource_errors
from test_topic_agent import context, resources, finished_conversation
import topic_agent
from budget import RunBudget


def test_default_scope_uses_only_home_and_explicit_repositories():
    policy = resolve(
        {
            "repository": "Acme/automations",
            "repositories": {"Acme/config": {"checkout": "/existing/config"}},
        }
    )
    assert policy == {
        "scope": "customer_stack",
        "repositories": ["Acme/automations", "Acme/config"],
    }
    assert permits(policy, "acme/CONFIG")
    assert not permits(policy, "OpenHands/software-agent-sdk")
    assert not permits(policy, "Acme/unregistered")
    assert permits(
        resolve({"repository": "Acme/automations", "repair_scope": "any_authorized"}),
        "OpenHands/software-agent-sdk",
    )


@pytest.mark.parametrize("scope", ["anywhere", None, False, {}, []])
def test_unknown_scope_is_rejected(scope):
    with pytest.raises(ValueError, match="repair_scope"):
        resolve({"repository": "Acme/app", "repair_scope": scope})


@pytest.mark.parametrize("scope", ["customer_stack", "any_authorized"])
def test_scope_and_configuration_guidance_reach_repair_review_and_continuation(
    tmp_path, scope
):
    ctx = context(tmp_path)
    ctx["repair_policy"] = resolve({"repository": "Acme/app", "repair_scope": scope})
    for review, feedback in [
        (False, None),
        (True, None),
        (False, {"feedback": "Fix the replay"}),
    ]:
        current = ctx | {"repair_attempt": 2, "max_repair_attempts": 2}
        if feedback:
            current["review_feedback"] = feedback
        prompt = topic_agent.prompt(
            current, tmp_path / "context.json", tmp_path, review=review
        )
        assert instructions(ctx["repair_policy"]) in prompt
        assert "source of truth" in prompt
        assert "Do not change live settings" in prompt
        assert "not a restriction" not in prompt


def test_out_of_scope_outputs_block_review_without_writing_to_other_repositories(
    tmp_path, monkeypatch
):
    ctx = context(tmp_path)
    ctx["repair_policy"] = resolve({"repository": "Example/automation"})
    rows = resources()
    monkeypatch.setattr(
        topic_agent, "discover", lambda *a: {"resources": rows, "errors": []}
    )
    recorded, held = [], []
    monkeypatch.setattr(
        topic_agent,
        "record_evidence",
        lambda c, o, phase, rs: recorded.extend(rs) or [],
    )
    monkeypatch.setattr(topic_agent, "hold_drafts", lambda rs: held.extend(rs) or [])
    monkeypatch.setattr(
        topic_agent,
        "run_conversation",
        lambda *a: pytest.fail("scope violation must block review"),
    )
    result = topic_agent.finish(
        {}, ctx, tmp_path, RunBudget({}), {"status": "finished"}
    )
    assert result["status"] == "review_incomplete"
    assert len(resource_errors(ctx["repair_policy"], rows)) == 2
    assert any("outside customer_stack" in x for x in result["link_errors"])
    assert {r["repository"] for r in recorded + held} == {"Example/automation"}


def test_any_authorized_accepts_evidenced_cross_repository_outputs(
    tmp_path, monkeypatch
):
    ctx = context(tmp_path)
    ctx["repair_policy"] = resolve(
        {"repository": "Example/automation", "repair_scope": "any_authorized"}
    )
    monkeypatch.setattr(
        topic_agent, "discover", lambda *a: {"resources": resources(), "errors": []}
    )
    monkeypatch.setattr(topic_agent, "record_evidence", lambda *a: [])
    monkeypatch.setattr(topic_agent, "run_conversation", finished_conversation)
    result = topic_agent.finish(
        {}, ctx, tmp_path, RunBudget({}), {"status": "finished"}
    )
    assert result["status"] == "reviewed"
    assert result["review_decision"]["decision"] == "accepted"


def test_out_of_scope_recurrence_cannot_publish_a_comment(tmp_path, monkeypatch):
    import tracked_work
    from test_tracked_work import work

    existing = [work(1, repository="Upstream/sdk")]
    topic = {"id": "one", "existing_work_urls": [existing[0]["url"]]}
    monkeypatch.setattr(
        tracked_work, "command", lambda *a: pytest.fail("out-of-scope GitHub write")
    )
    with pytest.raises(ValueError, match="outside customer_stack"):
        tracked_work.comment(
            topic,
            existing,
            [],
            None,
            tmp_path,
            policy=resolve({"repository": "Acme/app"}),
        )
