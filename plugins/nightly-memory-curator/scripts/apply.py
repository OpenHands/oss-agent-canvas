#!/usr/bin/env python3
"""Apply curation verdicts to the memory store, enforce the budget, render the skill.

The growth guarantee lives here, not in the prompt. The model proposes verdicts;
this script validates them, applies decay, evicts to fit hard caps, and refuses
to write anything that still exceeds budget.

Scope of that guarantee, stated honestly: this stops malformed, oversized and
structure-injecting verdicts. It is NOT a sandbox. The curator runs as the same
user that owns the skill directory and has a shell, so it could bypass this
script and write SKILL.md directly. Treat the caps as a well-enforced convention
against an agent that is going along with the process, not as a boundary against
one that is not.

Exit codes: 0 applied, 1 rejected (nothing written), 2 bad input.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from datetime import date, datetime, timedelta

OH = "/var/lib/agent-canvas/.openhands"
STORE_DIR = os.environ.get("MEMORY_STORE_DIR", f"{OH}/agent-memory")
SKILL_DIR = os.environ.get("MEMORY_SKILL_DIR", f"{OH}/skills/oss-operating-memory")

MEMORY_FILE = f"{STORE_DIR}/memory.json"
ARCHIVE_FILE = f"{STORE_DIR}/archive.json"
STATE_FILE = f"{STORE_DIR}/state.json"
DIGEST_FILE = f"{STORE_DIR}/digest.json"
VERDICTS_FILE = f"{STORE_DIR}/verdicts.json"
SKILL_FILE = f"{SKILL_DIR}/SKILL.md"

# Budget. The rendered body is injected into EVERY automation's system prompt,
# so every byte is paid on every run of all ~27 automations. Keep it small.
MAX_ENTRIES = int(os.environ.get("MEMORY_MAX_ENTRIES", "40"))
MAX_RENDER_BYTES = int(os.environ.get("MEMORY_MAX_RENDER_BYTES", "6000"))
MAX_CLAIM_CHARS = 220
MAX_NEW_PER_RUN = int(os.environ.get("MEMORY_MAX_NEW_PER_RUN", "5"))
DECAY_DAYS = int(os.environ.get("MEMORY_DECAY_DAYS", "60"))

MAX_SCOPE_CHARS = 80

VALID_VERDICTS = {"NEW", "DUPLICATE", "REINFORCES", "CONTRADICTS", "SUPERSEDES"}
ID_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
TODAY = date.today()


def one_line(text: str) -> str:
    """Collapse to a single line of plain text.

    The rendered file is injected into every automation's system prompt, so a
    claim must never be able to introduce structure. Without this, a claim well
    under the character cap can still smuggle in a markdown heading and read as
    a new section of instructions rather than as one bullet.
    """
    return " ".join(str(text).split()).lstrip("#-*>`").strip()


def load(path, default):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def fail(msg: str) -> int:
    print(f"REJECTED: {msg}", file=sys.stderr)
    return 1


def entry_score(e: dict) -> float:
    """Eviction ranking: reinforcement weighted by recency. Lowest is evicted."""
    try:
        last = datetime.fromisoformat(e.get("last_confirmed", "1970-01-01")).date()
    except ValueError:
        last = date(1970, 1, 1)
    age_days = max((TODAY - last).days, 0)
    recency = max(0.1, 1.0 - (age_days / max(DECAY_DAYS, 1)))
    return float(e.get("hit_count", 1)) * recency


def render(entries: list[dict]) -> str:
    """Render the always-on skill. No `triggers:` key -- per the SDK, a skill
    with trigger=None has its full body injected into <REPO_CONTEXT> for every
    conversation. Adding triggers here would make the memory load only on
    keyword match, which would silently defeat the whole thing."""
    lines = [
        "---",
        "name: oss-operating-memory",
        "description: Operating lessons learned from previous automation runs on this box. Always loaded.",
        "---",
        "",
        "# Operating memory",
        "",
        "Lessons distilled from earlier automation runs on this VM. Each was",
        "confirmed by real runs; the count shows how many times it recurred.",
        "Prefer these over re-deriving the same conclusion.",
        "",
    ]
    by_scope: dict[str, list[dict]] = {}
    for e in entries:
        by_scope.setdefault(e.get("scope", "global"), []).append(e)

    # Re-sanitise on the way out as well as on the way in: memory.json is a plain
    # file on disk, so this is the last point before the text becomes part of
    # every automation's system prompt.
    for scope in sorted(by_scope, key=lambda s: (s != "global", s)):
        lines.append(f"## {one_line(scope)[:MAX_SCOPE_CHARS] or 'global'}")
        for e in sorted(by_scope[scope], key=entry_score, reverse=True):
            lines.append(f"- {one_line(e['claim'])[:MAX_CLAIM_CHARS]} _(x{e.get('hit_count', 1)})_")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    digest = load(DIGEST_FILE, None)
    if digest is None:
        return fail(f"no digest at {DIGEST_FILE}; run digest.py first")

    verdicts = load(VERDICTS_FILE, None)
    if not isinstance(verdicts, list):
        return fail(f"{VERDICTS_FILE} must contain a JSON list of verdict objects")

    memory = load(MEMORY_FILE, [])
    archive = load(ARCHIVE_FILE, [])
    by_id = {e["id"]: e for e in memory}
    valid_ids = set(by_id)
    today_iso = TODAY.isoformat()

    added, updated, deleted, ignored = [], [], [], []
    new_count = 0

    for v in verdicts:
        if not isinstance(v, dict):
            return fail(f"verdict is not an object: {v!r}")
        kind = str(v.get("verdict", "")).upper()
        if kind not in VALID_VERDICTS:
            return fail(f"unknown verdict {kind!r}; must be one of {sorted(VALID_VERDICTS)}")

        if kind == "DUPLICATE":
            ignored.append(v.get("target_id") or v.get("id") or "?")
            continue

        if kind == "REINFORCES":
            tid = v.get("target_id")
            if tid not in valid_ids:
                return fail(f"REINFORCES target_id {tid!r} not in memory")
            e = by_id[tid]
            e["hit_count"] = int(e.get("hit_count", 1)) + 1
            e["last_confirmed"] = today_iso
            ev = list(dict.fromkeys((e.get("evidence") or []) + (v.get("evidence") or [])))
            e["evidence"] = ev[-5:]
            updated.append(tid)
            continue

        if kind == "CONTRADICTS":
            tid = v.get("target_id")
            if tid not in valid_ids:
                return fail(f"CONTRADICTS target_id {tid!r} not in memory")
            e = by_id.pop(tid)
            valid_ids.discard(tid)
            memory = [m for m in memory if m["id"] != tid]
            e["retired_on"] = today_iso
            e["retired_reason"] = one_line(v.get("reason", ""))[:300]
            archive.append(e)
            deleted.append(tid)
            # A contradiction may also carry a replacement claim. The retired id
            # is free again, so default to it rather than demanding a new one --
            # requiring an `id` here made the documented correction path fail.
            if not v.get("claim"):
                continue
            v = {**v, "id": v.get("id") or tid}
            kind = "NEW"

        if kind == "SUPERSEDES":
            tid = v.get("target_id")
            if tid not in valid_ids:
                return fail(f"SUPERSEDES target_id {tid!r} not in memory")
            e = by_id[tid]
            claim = one_line(v.get("claim", ""))
            if not claim:
                return fail(f"SUPERSEDES {tid!r} requires a replacement claim")
            if len(claim) > MAX_CLAIM_CHARS:
                return fail(f"claim for {tid!r} is {len(claim)} chars (max {MAX_CLAIM_CHARS})")
            e["claim"] = claim
            e["scope"] = one_line(v.get("scope") or e.get("scope") or "global")[:MAX_SCOPE_CHARS] or "global"
            e["last_confirmed"] = today_iso
            e["hit_count"] = int(e.get("hit_count", 1)) + 1
            updated.append(tid)
            continue

        if kind == "NEW":
            new_count += 1
            if new_count > MAX_NEW_PER_RUN:
                return fail(f"{new_count} NEW entries proposed; cap is {MAX_NEW_PER_RUN} per run")
            eid = str(v.get("id", "")).strip()
            claim = one_line(v.get("claim", ""))
            if not ID_RE.match(eid):
                return fail(f"invalid id {eid!r}; must be kebab-case")
            if eid in valid_ids:
                return fail(f"NEW id {eid!r} already exists; use REINFORCES or SUPERSEDES")
            if not claim:
                return fail(f"NEW {eid!r} has an empty claim")
            if len(claim) > MAX_CLAIM_CHARS:
                return fail(f"claim for {eid!r} is {len(claim)} chars (max {MAX_CLAIM_CHARS})")
            entry = {
                "id": eid,
                "claim": claim,
                "scope": one_line(v.get("scope") or "global")[:MAX_SCOPE_CHARS] or "global",
                "why": one_line(v.get("why", ""))[:300],
                "evidence": (v.get("evidence") or [])[:5],
                "hit_count": 1,
                "created": today_iso,
                "last_confirmed": today_iso,
            }
            memory.append(entry)
            by_id[eid] = entry
            valid_ids.add(eid)
            added.append(eid)

    # --- Decay: anything not reconfirmed within DECAY_DAYS leaves the hot set.
    cutoff = TODAY - timedelta(days=DECAY_DAYS)
    kept, decayed = [], []
    for e in memory:
        try:
            last = datetime.fromisoformat(e.get("last_confirmed", today_iso)).date()
        except ValueError:
            last = TODAY
        if last < cutoff:
            e["retired_on"] = today_iso
            e["retired_reason"] = f"not reconfirmed in {DECAY_DAYS} days"
            archive.append(e)
            decayed.append(e["id"])
        else:
            kept.append(e)
    memory = kept

    # --- Enforce caps by eviction, lowest-value first.
    evicted = []
    memory.sort(key=entry_score, reverse=True)
    while len(memory) > MAX_ENTRIES:
        e = memory.pop()
        e["retired_on"] = today_iso
        e["retired_reason"] = f"over entry cap ({MAX_ENTRIES})"
        archive.append(e)
        evicted.append(e["id"])

    body = render(memory)
    while len(body.encode()) > MAX_RENDER_BYTES and memory:
        e = memory.pop()
        e["retired_on"] = today_iso
        e["retired_reason"] = f"over byte cap ({MAX_RENDER_BYTES})"
        archive.append(e)
        evicted.append(e["id"])
        body = render(memory)

    size = len(body.encode())
    if size > MAX_RENDER_BYTES:
        return fail(f"rendered memory is {size}B, over the {MAX_RENDER_BYTES}B cap")

    if os.environ.get("MEMORY_DRY_RUN") == "1":
        print("--- DRY RUN, nothing written ---")
        print(f"added={added} updated={updated} deleted={deleted}")
        print(f"decayed={decayed} evicted={evicted} ignored_duplicates={len(ignored)}")
        print(f"entries={len(memory)}/{MAX_ENTRIES}  rendered={size}/{MAX_RENDER_BYTES}B")
        print("\n" + body)
        return 0

    os.makedirs(STORE_DIR, exist_ok=True)
    os.makedirs(SKILL_DIR, exist_ok=True)

    def atomic(path: str, text: str) -> None:
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            fh.write(text)
        shutil.move(tmp, path)

    atomic(MEMORY_FILE, json.dumps(memory, indent=1) + "\n")
    atomic(ARCHIVE_FILE, json.dumps(archive[-500:], indent=1) + "\n")
    atomic(SKILL_FILE, body)

    # Watermark advances only on a fully successful apply, so a mid-run failure
    # re-reads the same conversations next time instead of skipping them.
    state = load(STATE_FILE, {"watermark": 0.0, "seen": []})
    state["watermark"] = digest.get("new_watermark", state.get("watermark", 0.0))
    state["seen"] = (state.get("seen", []) + digest.get("selected_ids", []))[-400:]
    state["last_run"] = datetime.now().isoformat()
    atomic(STATE_FILE, json.dumps(state, indent=1) + "\n")

    print(f"applied: +{len(added)} ~{len(updated)} -{len(deleted)} "
          f"decayed={len(decayed)} evicted={len(evicted)} dup_ignored={len(ignored)}")
    print(f"entries={len(memory)}/{MAX_ENTRIES}  rendered={size}/{MAX_RENDER_BYTES}B")
    print(f"wrote {SKILL_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
