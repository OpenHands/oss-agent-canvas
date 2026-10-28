# Controlled review publication

The event reviewer gives each fresh analyzing conversation only a short-lived,
repository-scoped gateway credential. It resolves the selected profile's LLM but
uses an explicit agent with terminal, file editor and task tracker, without the
profile's secrets or MCP bindings. Existing privileged conversations are not reused.
The native Agent Server must provide an isolated container for each conversation.

The agent downloads the requested commit's source archive, reads GitHub through
scoped GET requests, runs tests and submits one structured proposal. Trusted worker
code owns the GitHub credential, checks the live head and existing reviews, builds
the disclosure/verdict and immutable commit target, and verifies the publication
response and readback. It uses COMMENT for findings, scope stops and self-approvals.
Unattachable findings go in the body; explicitly rejected inline positions get one
body-only fallback. Uncertain or malformed accepted writes require operator review
and are never automatically replaced. Publication failure is reported separately
from successful analysis; agent completion text cannot assert GitHub acceptance.

Before rollout, pause the old intake and let its privileged conversations finish;
then replace the worker while retaining the publication ledger.

This is a single-VM prototype. `REVIEW_STATE_DIR` defaults to
`~/.local/state/review-publication` and must persist across runs. Per-PR file locks
cover analysis and publication. `REVIEW_GATEWAY_HOST` defaults to the VM's Docker
bridge address `172.17.0.1`; the container must reach that private address. Gateway
requests bypass HTTP proxies. No host credentials or state directory may be mounted
in analyzing containers. The agent budget is 180 seconds and 30 steps; publication
requires a finished conversation with a validated proposal.

GitHub has no atomic compare-head-and-create-review operation. Repeated head checks
reduce the race, commit_id always remains the requested head, and movement detected
after the POST prevents a success claim. This cannot guarantee the branch remains
unchanged during the network request. Archives are limited to 64 MB compressed and
250 MB expanded, with no submodule/LFS checkout or symlink extraction.

Run deterministic publication/gateway checks with:

```bash
python3 -m unittest discover -s plugins/github-pr-reviewer-events/tests -v
```

Behavioral baseline/candidate comparisons live in the separate
`evals/automation-repair` prototype. These changes do not install or enable a live
reviewer bundle.
