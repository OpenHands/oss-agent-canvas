"""Value-free process credential audit for the health review."""

import json
import re
import shlex
import signal
import sys
import time
from pathlib import Path

CREDENTIAL_OPTIONS = (
    b"--connection-token", b"--api-key", b"--api_key", b"--token",
    b"--password", b"--client-secret",
)
PROGRAMS = {
    b"openvscode-server": "openvscode-server",
    b"server-main.js": "openvscode-server",
    b"python": "python", b"python3": "python", b"python3.12": "python",
    b"node": "node", b"sh": "sh", b"bash": "bash",
}
MAX_CMDLINE_BYTES = 65536
MAX_FINDINGS = 50


def audit_processes(proc_root=Path("/proc")):
    result = {
        "scope": "agent-server process namespace only; recognized options only",
        "status": "complete", "scanned": 0, "unreadable": 0,
        "truncated": 0, "omitted": 0, "findings": [],
    }
    deadline = time.monotonic() + 2
    try:
        entries = sorted(path for path in proc_root.iterdir() if path.name.isdecimal())
    except OSError:
        result["status"] = "unavailable"
        return result
    if not entries:
        result["status"] = "unavailable"
    for path in entries:
        if time.monotonic() >= deadline:
            result["status"] = "partial"
            break
        try:
            with (path / "cmdline").open("rb") as stream:
                raw = stream.read(MAX_CMDLINE_BYTES + 1)
        except OSError:
            result["unreadable"] += 1
            continue
        if len(raw) > MAX_CMDLINE_BYTES:
            result["truncated"] += 1
            continue
        result["scanned"] += 1
        argv = raw.rstrip(b"\0").split(b"\0")
        options = []
        for option in CREDENTIAL_OPTIONS:
            for index, arg in enumerate(argv[1:], 1):
                if (arg.startswith(option + b"=") and arg != option + b"=") or (
                    arg == option and index + 1 < len(argv)
                    and argv[index + 1] and not argv[index + 1].startswith(b"-")
                ):
                    options.append(option.decode("ascii"))
                    break
        if not options:
            continue
        if len(result["findings"]) >= MAX_FINDINGS:
            result["omitted"] += 1
            continue
        program = PROGRAMS.get(argv[0].rsplit(b"/", 1)[-1], "other")
        if program in ("sh", "bash", "node") and len(argv) > 1:
            program = PROGRAMS.get(argv[1].rsplit(b"/", 1)[-1], program)
        # Only fixed labels leave this function, never argv-derived strings.
        result["findings"].append({
            "pid": int(path.name), "program": program, "options": options,
        })
    if result["unreadable"] or result["truncated"] or result["omitted"]:
        result["status"] = "partial"
    return result


def run_hook(proc_root=Path("/proc")):
    def timed_out(signum, frame):
        raise RuntimeError

    # Finish with a denial before the SDK's fail-open hook timeout.
    signal.signal(signal.SIGALRM, timed_out)
    signal.alarm(3)
    try:
        event = json.load(sys.stdin)
        command = event.get("tool_input", {}).get("command", "")
        if not isinstance(command, str):
            raise ValueError
        # Conservative literal matching also catches quoted shell wrappers.
        if re.search(r"(?<![\w-])ps(?![\w-])", command):
            reason = (
                "Raw process listings are blocked. No part of this command was run; "
                "run unrelated diagnostics separately. Value-free credential-option "
                "audit (option presence is not proof of a live credential): "
                + json.dumps(audit_processes(proc_root))
            )
            print(json.dumps({"decision": "deny", "reason": reason}))
        else:
            print(json.dumps({"decision": "allow"}))
    except Exception:
        print(json.dumps({
            "decision": "deny",
            "reason": "Process audit unavailable; command not run. No raw fallback.",
        }))
    finally:
        signal.alarm(0)


def get_process_audit_hook():
    # The embedded hook runs with the server's system Python, without SDK imports.
    from openhands.sdk.hooks import HookConfig, HookDefinition, HookMatcher

    command = "python3 -c " + shlex.quote(Path(__file__).read_text())
    command += " 2>/dev/null || exit 2"
    return HookConfig(pre_tool_use=[HookMatcher(
        matcher="terminal",
        hooks=[HookDefinition(command=command, timeout=5)],
    )])


if __name__ == "__main__":
    run_hook()
