#!/usr/bin/env python3
"""Read-only digest of recent Agent Canvas conversations.

Emits a compact JSON file for the curation step. Never feeds raw event logs to
an LLM -- a single conversation on this box reached 251 events / 5M prompt
tokens, so everything is reduced mechanically here first.

Selection is signal-first: failed runs teach more than successful ones.

Deliberately excluded: conversation message text. Runs of the PR reviewer, issue
resolver and Slack monitor carry text written by people outside the team --
GitHub issue and PR bodies from any external account, and Slack messages. That
text would otherwise flow into the curator, and anything the curator accepts
becomes a standing instruction in the system prompt of every automation on this
box, including ones holding GITHUB_TOKEN. So the digest carries only what the
platform itself produced: run status, error detail, exit codes and cost. The
automation's own name supplies the context that the task text used to.

Residual risk worth knowing: error_detail and captured commands can still quote
remote content, so this narrows the path rather than closing it. Entries are
meant to be reviewed before they go live.
"""

from __future__ import annotations

import glob
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone

OH = "/var/lib/agent-canvas/.openhands"
CONV_DIR = f"{OH}/agent-canvas/dev_conversations"
DB = f"{OH}/automation/automations.db"
STORE_DIR = os.environ.get("MEMORY_STORE_DIR", f"{OH}/agent-memory")
STATE_FILE = f"{STORE_DIR}/state.json"

MAX_CONVERSATIONS = int(os.environ.get("MEMORY_MAX_CONVERSATIONS", "20"))
MAX_ERR_CHARS = 500
MAX_SIGNALS = 6
MIN_CMD_LEN_FOR_REPEAT = 25

# Per-run workspace paths are long, unique per run, and carry no meaning across
# runs; collapsing them keeps the distinguishing part of a command inside the cap.
WS_PATH_RE = re.compile(r"/var/lib/agent-canvas/\.openhands/agent-canvas/workspaces/\S*?/")

# Redaction. base_state.json carries encrypted api_key blobs inline, and
# secrets are injected into conversations as env vars, so anything that smells
# like a credential is stripped before it can reach the curation prompt.
SECRET_KEY_RE = re.compile(r"(api[_-]?key|token|secret|password|authorization|bearer)", re.I)
SECRET_VAL_RE = re.compile(
    r"(gAAAAA[\w\-=]{16,}"           # fernet (agent-canvas encrypted values)
    r"|gh[pousr]_[A-Za-z0-9]{16,}"   # github tokens
    r"|xox[baprs]-[A-Za-z0-9-]{10,}" # slack tokens
    r"|sk-[A-Za-z0-9]{16,}"          # openai-style
    r"|eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"  # jwt
    r")"
)


def scrub(text: str) -> str:
    if not text:
        return ""
    return WS_PATH_RE.sub("<ws>/", SECRET_VAL_RE.sub("[REDACTED]", text))


def walk_find(obj, pred, out, limit=200):
    """Recursively collect values whose key matches pred. Schema-agnostic on
    purpose: the event schema is not a stable contract, so we never index into
    a fixed shape."""
    if len(out) >= limit:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            if pred(k, v):
                out.append((k, v))
            walk_find(v, pred, out, limit)
    elif isinstance(obj, list):
        for v in obj:
            walk_find(v, pred, out, limit)


def load_state() -> dict:
    try:
        with open(STATE_FILE) as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"watermark": 0.0, "seen": []}


def conversation_signals(conv_path: str) -> list[str]:
    """Mechanical failure signals: non-zero exits and repeated identical
    commands (the usual shape of an agent stuck in a loop)."""
    signals: list[str] = []
    commands: list[str] = []
    event_files = sorted(glob.glob(os.path.join(conv_path, "events", "*.json")))
    for ef in event_files:
        try:
            with open(ef) as fh:
                ev = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue

        cmds: list = []
        walk_find(ev, lambda k, v: k == "command" and isinstance(v, str), cmds, 3)
        cmd_text = (scrub(cmds[0][1]).replace("\n", " ")[:160]) if cmds else "(unknown)"

        exits: list = []
        walk_find(ev, lambda k, v: k in ("exit_code", "returncode") and isinstance(v, int), exits, 20)
        for _, code in exits:
            # -1 is the interrupted/no-status marker and fires constantly; it says
            # nothing on its own, so only genuine non-zero exits become signals.
            if code not in (0, -1):
                signals.append(f"exit={code} cmd={cmd_text}")

        for _, c in cmds:
            # Scrub before truncating, or the collapsed path never shortens anything.
            flat = scrub(c).replace("\n", " ")[:160]
            if len(flat) >= MIN_CMD_LEN_FOR_REPEAT:
                commands.append(flat)

    counts: dict[str, int] = {}
    for c in commands:
        counts[c] = counts.get(c, 0) + 1
    for cmd, n in counts.items():
        if n >= 4:
            signals.append(f"repeated x{n}: {scrub(cmd)}")

    return list(dict.fromkeys(signals))[:MAX_SIGNALS]


def main() -> int:
    os.makedirs(STORE_DIR, exist_ok=True)
    state = load_state()
    watermark = float(state.get("watermark") or 0.0)
    seen_ids = set(state.get("seen") or [])

    if not os.path.isdir(CONV_DIR):
        print(f"FATAL: no conversation dir at {CONV_DIR}", file=sys.stderr)
        return 2

    # Read-only so a live service is never disturbed.
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    runs: dict[str, dict] = {}
    for r in db.execute(
        "SELECT r.conversation_id, r.status, r.error_detail, r.cost, r.completed_at,"
        "       a.name AS automation_name "
        "FROM automation_runs r LEFT JOIN automations a ON a.id = r.automation_id "
        "WHERE r.conversation_id IS NOT NULL"
    ):
        d = dict(r)
        runs[str(d["conversation_id"]).replace("-", "")] = d
    db.close()

    candidates = []
    for path in glob.glob(f"{CONV_DIR}/*"):
        cid = os.path.basename(path)
        base = os.path.join(path, "base_state.json")
        if not os.path.exists(base):
            continue
        mtime = os.path.getmtime(base)
        if mtime <= watermark or cid in seen_ids:
            continue
        candidates.append((mtime, cid, path))

    # Signal-first ranking: errors, then stuck/timeout, then cost, then recency.
    def rank(item):
        mtime, cid, path = item
        run = runs.get(cid, {})
        status = (run.get("status") or "").upper()
        has_err = bool(run.get("error_detail"))
        failed = status not in ("", "SUCCESS", "COMPLETED", "FINISHED")
        cost = float(run.get("cost") or 0.0)
        return (0 if has_err else 1, 0 if failed else 1, -cost, -mtime)

    candidates.sort(key=rank)
    selected = candidates[:MAX_CONVERSATIONS]

    digests = []
    for mtime, cid, path in selected:
        run = runs.get(cid, {})
        try:
            with open(os.path.join(path, "base_state.json")) as fh:
                base = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue

        digests.append(
            {
                "conversation_id": cid,
                "automation": run.get("automation_name") or "(interactive)",
                "status": run.get("status") or "unknown",
                "error": scrub(str(run.get("error_detail") or ""))[:MAX_ERR_CHARS],
                "cost": round(float(run.get("cost") or 0.0), 4),
                "execution_status": base.get("execution_status"),
                "stuck_detection": base.get("stuck_detection"),
                "failure_signals": [scrub(s) for s in conversation_signals(path)],
                "finished_at": datetime.fromtimestamp(mtime, timezone.utc).isoformat(),
            }
        )

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "considered": len(candidates),
        "selected": len(digests),
        "new_watermark": max([m for m, _, _ in selected], default=watermark),
        "selected_ids": [d["conversation_id"] for d in digests],
        "conversations": digests,
    }
    dest = os.path.join(STORE_DIR, "digest.json")
    with open(dest, "w") as fh:
        json.dump(out, fh, indent=1)

    print(f"digest: {len(digests)} conversations selected of {len(candidates)} new")
    print(f"wrote {dest} ({os.path.getsize(dest)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
