# Local flow-1 setup

Flow-1 works through Laminar Signals. Its Cloud endpoint does not accept the
native monitor's general-purpose Chat Completions/Responses requests. This
isolated experiment tests the actual Signal engine without changing the monitor.

The running instance is http://localhost:15667. Its private working directory is
`~/experiments/laminar-flow1-local`. The approved Cloud key is stored with
owner-only permissions at `~/.config/laminar/flow1-cloud.env`; it expires November
15, 2026. It belongs to the Pro OpenHands Cloud workspace's Skills project.

From this directory, repeat the smoke test:

```bash
FLOW1_DATA_DIR="$HOME/experiments/laminar-flow1-local" \
  uv run --with lmnr==0.7.67 python smoke.py
```

The test ingests two synthetic traces locally, waits until they are readable,
then runs the Signal once on each. The backend settings cap each test at one
model step and 2,048 output tokens. An explicit hidden test failure must produce
an event; successful tests must produce `noEvent`. Other results fail the test.
Requests use the EE `signal-test` route; the older frontend `signals/execute`
helper returns a backend 404 in this build.

To recreate the instance:

```bash
python configure.py "$HOME/experiments/laminar-flow1-local"
cd "$HOME/experiments/laminar-flow1-local"
docker compose up -d
```

Wait for Postgres and ClickHouse initialization to finish in `docker compose
logs frontend` (several minutes on first boot). Sign in at localhost with
`flow1-local@example.test`, create a workspace/project, and read their IDs from
the URL/settings. Run `configure.py` from its source directory with the data
directory plus `--project-id` and `--workspace-id`. It creates a local ingest-only
key and the flow-1 profile, then configures model costs. Restart `app-server`
after first-time migrations. All exposed ports bind to loopback. No production
data is copied. `docker compose stop` stops the instance and retains its data.

The profile uses Custom / Chat Completions, `https://api.lmnr.ai/v1`, and model
`flow-1`. Cloud receives the trace portions requested by the Signal; the vendor
documents zero prompt/completion retention for this endpoint. Runs and traces
remain local. Cloud token usage is billed to the key's workspace.

## Cost measurement

Verified against Cloud billing on October 16, 2026: fresh input is $0.05/M
tokens, cached input $0.01/M, output $0.30/M. Cached tokens are a subset of total
input: subtract them before pricing fresh input. Reports retain the rate source,
verification date, token breakdown, elapsed time, returned model name, and raw
Laminar statistics. The independent decimal calculation is in
`estimatedInferenceCost`; this is usage value before credits, not a final invoice.
Cloud workspace Usage shows consumption and the remaining one-time credit.

Custom model cost entries cover `flow-1` and the currently returned
`flow-1-style-r3-mtp-vonly` under provider `openai`. Without them, Laminar returns
zero for an unknown model; that is not free inference. Recheck rates and model
aliases when updating the service. The latest captured synthetic results match
Laminar's own cost calculation: $0.00021034 and $0.00011804, with 5,824 cached
input tokens each. These tiny warm-cache cases are connectivity tests, not a
performance or quality benchmark.

EE frontend/app-server are pinned together at 0.1.825. The XML configuration is
from lmnr-ai/lmnr commit `7e8db4bbabcbb24b2208dfd9f230ed777b2583dc`.
Shared staging/production still need a reviewed upgrade and smoke test against
their existing databases; staging preparation is
https://github.com/OpenHands/saas-deploy/pull/1515.

References: https://laminar.sh/docs/platform/llm-profiles#run-self-hosted-signals-on-the-laminar-cloud-model,
https://laminar.sh/pricing.
