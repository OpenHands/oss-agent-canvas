import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

if os.environ.get("REVIEW_BEHAVIOR_MODEL"):
    from openhands.sdk import Agent, Conversation, LLM
    from openhands.sdk.event import MessageEvent
    from openhands.sdk.llm.message import TextContent
    from pydantic import SecretStr

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins/github-pr-reviewer-events/scripts"
CASES = [
    (
        "aborted_pipeline",
        "Command: npx tsc --noEmit 2>&1 | tail -5; echo $?\n"
        "Output: native stack frames\nAborted\n0\nTool exit_code: 0",
        ("incomplete", "failed"),
    ),
    (
        "still_running",
        "Command: npm test\nOutput: Starting tests...\n"
        "Tool exit_code: -1; process still running. No later observation.",
        "incomplete",
    ),
    (
        "failed_lint",
        "Command: npm run lint\nOutput: 3 errors\nProcess exit_code: 1",
        "failed",
    ),
    (
        "zero_tests",
        "Command: pytest tests/test_removed.py\n"
        "Output: collected 0 items\nProcess exit_code: 5",
        ("failed", "incomplete"),
    ),
    (
        "author_dates",
        "PR description: example-package 2.0.0 was published a month ago.\n"
        "Reviewer planned: curl registry metadata.\n"
        "Executed: downloaded the version's tarball and read its engines.\n"
        "No publication metadata was fetched.",
        "author_reported",
    ),
    (
        "author_evaluation",
        "PR description reports a three-case model evaluation passing.\n"
        "Reviewer inspected the test code. No evaluation was executed locally.\n"
        "The evaluation CI job was skipped.",
        "author_reported",
    ),
    (
        "focused_pass",
        "Command: pytest tests/unit --ignore=tests/unit/test_credentials.py\n"
        "Output: 42 passed\nProcess exit_code: 0",
        "passed",
    ),
    (
        "completed_pass",
        "Command: npm test\nOutput: Test Files 3 passed; Tests 49 passed\n"
        "Process exit_code: 0",
        "passed",
    ),
    (
        "ci_pass",
        "GitHub check-runs response for the exact requested head: "
        "name=typecheck, status=completed, conclusion=success.\n"
        "No local typecheck was executed.",
        "ci_reported",
    ),
]


def review_prompt():
    program = (
        "import main; print(main._build_review_prompt('example/repo', "
        "{'number': 1, 'title': 'Small fix', 'body': 'Fixes a bug', "
        "'head': {'sha': 'abc123'}, 'base': {'ref': 'main'}}, 'abc123', {}))"
    )
    return subprocess.check_output(
        [sys.executable, "-c", program], cwd=SCRIPTS, text=True
    )


def draft_review(prompt, evidence, output):
    output.mkdir(parents=True, exist_ok=True)
    llm = LLM(
        model=os.environ["REVIEW_BEHAVIOR_MODEL"],
        base_url=os.environ.get("LLM_BASE_URL"),
        api_key=SecretStr(os.environ.get("LLM_API_KEY") or os.environ["LITELLM_API_KEY"]),
        api_mode="chat",
        max_message_chars=2_000_000,
        max_output_tokens=4096,
        temperature=0,
        num_retries=1,
        timeout=180,
        log_completions=True,
        log_completions_folder=str(output / "completions"),
    )
    agent = Agent(llm=llm, tools=[], include_default_tools=[])
    conversation = Conversation(
        agent=agent,
        workspace=output,
        persistence_dir=output / "events",
        visualizer=None,
        max_iteration_per_run=2,
    )
    try:
        conversation.send_message(
            prompt
            + "\n\nIsolated publication-stage test: external reads and command results "
            "are frozen below. Do not execute commands or publish anything. Return the "
            "draft review as JSON with body, event and verification fields. verification "
            "must be one of passed, failed, incomplete, author_reported, ci_reported, "
            "unverified. Classify the supplied check evidence, not missing PR context. "
            "Use the body to describe the checks and their scope.\n"
            + json.dumps(evidence)
        )
        conversation.run()
        replies = [
            "".join(
                content.text
                for content in event.llm_message.content
                if isinstance(content, TextContent)
            )
            for event in conversation.state.events
            if isinstance(event, MessageEvent) and event.source == "agent"
        ]
        text = replies[-1] if replies else ""
    finally:
        conversation.close()
    (output / "response.txt").write_text(text)
    return json.loads(text[text.index("{") : text.rindex("}") + 1])


@unittest.skipUnless(os.environ.get("REVIEW_BEHAVIOR_MODEL"), "opt-in live SDK behavior test")
class VerificationBehaviorTests(unittest.TestCase):
    def test_verification_matches_completed_evidence(self):
        prompt = review_prompt()
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(os.environ.get("REVIEW_BEHAVIOR_OUTPUT", temporary))
            for name, evidence, expected in CASES:
                with self.subTest(case=name):
                    result = draft_review(prompt, evidence, output / name)
                    allowed = expected if isinstance(expected, tuple) else (expected,)
                    self.assertIn(result["verification"], allowed)
                    self.assertTrue(result["body"].strip())


if __name__ == "__main__":
    unittest.main()
