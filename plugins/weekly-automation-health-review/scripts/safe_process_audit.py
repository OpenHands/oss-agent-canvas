import json
from pathlib import Path

FLAGS = frozenset({
    "connection-token", "token", "access-token", "auth-token", "api-key",
    "apikey", "password", "passwd", "client-secret", "secret",
})
MAX_CMDLINE_BYTES = 1024 * 1024


def unavailable_inventory():
    return {
        "status": "unavailable", "processes_scanned": 0,
        "processes_unreadable": 0, "credential_arguments": [],
    }


def scan_processes(proc_root=Path("/proc")):
    try:
        entries = sorted(
            (p for p in proc_root.iterdir() if p.name.isascii() and p.name.isdecimal()),
            key=lambda p: int(p.name),
        )
    except OSError:
        return unavailable_inventory()

    if not entries:
        return unavailable_inventory()

    result = unavailable_inventory()
    result["status"] = "ok"
    for entry in entries:
        try:
            with (entry / "cmdline").open("rb") as stream:
                raw = stream.read(MAX_CMDLINE_BYTES + 1)
            if len(raw) > MAX_CMDLINE_BYTES:
                result["processes_unreadable"] += 1
                continue
        except OSError:
            result["processes_unreadable"] += 1
            continue
        result["processes_scanned"] += 1
        found = set()
        # Only fixed flag labels cross the output boundary; never argv content.
        for arg in raw.split(b"\0")[1:]:
            if not arg.startswith(b"--"):
                continue
            name = arg.split(b"=", 1)[0][2:].decode("ascii", errors="replace").replace("_", "-")
            if name in FLAGS:
                found.add(name)
        if found:
            result["credential_arguments"].append({"pid": int(entry.name), "flags": sorted(found)})
    if result["processes_unreadable"]:
        result["status"] = "partial"
    return result


def valid_inventory(value):
    if not isinstance(value, dict) or set(value) != {
        "status", "processes_scanned", "processes_unreadable", "credential_arguments",
    }:
        return False
    if value["status"] not in ("ok", "partial", "unavailable"):
        return False
    if any(
        type(value[key]) is not int or value[key] < 0
        for key in ("processes_scanned", "processes_unreadable")
    ):
        return False
    if not isinstance(value["credential_arguments"], list):
        return False
    for item in value["credential_arguments"]:
        if not isinstance(item, dict) or set(item) != {"pid", "flags"}:
            return False
        if type(item["pid"]) is not int or item["pid"] <= 0:
            return False
        if not isinstance(item["flags"], list) or not all(
            isinstance(flag, str) and flag in FLAGS for flag in item["flags"]
        ):
            return False
    return True


def audit_command():
    return "python3 - <<'SAFE_PROCESS_AUDIT'\n" + Path(__file__).read_text() + "\nSAFE_PROCESS_AUDIT"


def collect_process_inventory(workspace):
    try:
        result = workspace.execute_command(audit_command(), timeout=30)
        if result.exit_code == 0 and not result.stderr:
            value = json.loads(result.stdout)
            if valid_inventory(value):
                return value
    except Exception:
        pass
    return unavailable_inventory()


if __name__ == "__main__":
    try:
        inventory = scan_processes()
    except Exception:
        inventory = unavailable_inventory()
    print(json.dumps(inventory, sort_keys=True))
