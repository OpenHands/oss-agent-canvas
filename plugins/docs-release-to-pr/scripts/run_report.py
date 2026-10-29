"""Append runner-observed activity without replacing the agent's answer."""

import json
import re
import shlex

from openhands.sdk.conversation import ConversationExecutionStatus


_SNAPSHOT = r'''
import hashlib
import json
import os
import stat
import subprocess
import sys


def snapshot(root):
    options = ["-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
               "-c", "core.untrackedCache=false"]

    def git(*args):
        return subprocess.check_output(
            ["git", *options, "-C", root, *args], stderr=subprocess.DEVNULL,
            env=dict(os.environ, GIT_OPTIONAL_LOCKS="0", GIT_PAGER="cat",
                     GIT_NO_LAZY_FETCH="1", GIT_ALLOW_PROTOCOL=""), timeout=15,
        )

    # Disabling content filters also makes status depend on index cache freshness.
    try:
        keys = git("config", "--null", "--name-only", "--get-regexp",
                   r"^filter\..*\.(clean|smudge|process|required)$")
    except subprocess.CalledProcessError as error:
        if error.returncode != 1:
            raise
        keys = b""
    if keys:
        return {"available": False}

    head = git("rev-parse", "HEAD")
    try:
        branch = git("symbolic-ref", "--quiet", "HEAD")
    except subprocess.CalledProcessError as error:
        if error.returncode != 1:
            raise
        branch = b"(detached)"
    refs = git("for-each-ref", "--format=%(refname) %(objectname)", "refs/heads/")
    index = git("diff", "--no-ext-diff", "--no-textconv", "--cached", "--raw", "--no-abbrev", "--ignore-submodules=all", "HEAD")
    status = git("status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=all")
    paths = set(git("ls-files", "-z", "--modified", "--deleted", "--others",
                    "--exclude-standard").split(b"\0"))
    paths.update(git("diff", "--no-ext-diff", "--no-textconv", "--cached",
                     "--name-only", "-z", "HEAD").split(b"\0"))
    submodules = {entry.split(b"\t", 1)[1] for entry in git("ls-files", "--stage", "-z").split(b"\0")
                  if entry.startswith(b"160000 ")}
    paths.difference_update(submodules)
    files = []
    for raw in sorted(paths - {b""}):
        path = os.path.join(root, os.fsdecode(raw))
        digest = hashlib.sha256()
        if not os.path.lexists(path):
            fingerprint = "deleted"
        else:
            mode = os.lstat(path).st_mode
            if stat.S_ISLNK(mode):
                digest.update(os.fsencode(os.readlink(path)))
            elif stat.S_ISREG(mode):
                with open(path, "rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
            else:
                raise ValueError("unsupported path type")
            fingerprint = str(mode) + ":" + digest.hexdigest()
        files.append((hashlib.sha256(raw).hexdigest(), fingerprint))
    return {
        "available": True, "dirty": bool(status),
        "branch": hashlib.sha256(branch).hexdigest(),
        "HEAD": hashlib.sha256(head).hexdigest(),
        "local branches": hashlib.sha256(refs).hexdigest(),
        "index": hashlib.sha256(index).hexdigest(),
        "worktree": hashlib.sha256(status + json.dumps(files).encode()).hexdigest(),
    }


results = []
for root in json.loads(sys.argv[1]):
    try:
        results.append(snapshot(root))
    except (OSError, ValueError, subprocess.SubprocessError):
        results.append({"available": False})
print(json.dumps(results))
'''


def snapshot_repositories(workspace, repo_dirs):
    if not repo_dirs:
        return []
    command = "python3 -I -c {} {}".format(
        shlex.quote(_SNAPSHOT), shlex.quote(json.dumps(repo_dirs))
    )
    try:
        result = workspace.execute_command(command, timeout=60)
        if result.exit_code != 0 or result.timeout_occurred:
            raise ValueError("snapshot failed")
        snapshots = json.loads(result.stdout)
        if not isinstance(snapshots, list) or len(snapshots) != len(repo_dirs):
            raise ValueError("invalid snapshots")
        return snapshots
    except Exception:
        return [{"available": False} for _ in repo_dirs]


def read_events(workspace, conversation_id):
    events, seen = [], set()
    page = None
    try:
        while True:
            params = {"limit": 100}
            if page:
                params["page_id"] = page
            response = workspace.client.get(
                f"/api/conversations/{conversation_id}/events/search", params=params
            )
            response.raise_for_status()
            data = response.json()
            events.extend(data["items"])
            page = data.get("next_page_id")
            if not page:
                return events, True
            if page in seen:
                return events, False
            seen.add(page)
    except Exception:
        return events, False


def build_report(events, before, after, *, history_complete):
    unique = {}
    for event in events:
        item = event if isinstance(event, dict) else event.model_dump(mode="json")
        unique[item["id"]] = item
    terminal, edits, labels = 0, 0, 0
    for item in unique.values():
        action = item.get("action") or {}
        if action.get("kind") == "TerminalAction":
            terminal += 1
            command = action.get("command", "")
            if (re.search(r"\bgh\s+issue\s+edit\b", command)
                    and re.search(r"--(?:add|remove)-label\b", command)) or (
                re.search(r"\bcurl\b", command)
                and re.search(r"api\.github\.com/repos/[^/\s]+/[^/\s]+/issues/\d+/labels\b", command)
                and re.search(r"(?:-X\s*|--request[=\s]+)['\"]?(?:POST|PUT|DELETE)\b", command)
            ):
                labels += 1
        elif action.get("kind") == "FileEditorAction" and action.get("command") != "view":
            edits += 1

    lines = [
        "## Runner activity report", "",
        "Generated by the OpenHands automation runner, not the model. This report "
        "qualifies the preceding agent answer, including any claim of no writes or "
        "pre-existing edits. Historical messages have not been changed.", "",
        "Agent execution finished before report delivery; no further model run was requested.",
        "History complete (persisted pages read)." if history_complete else
        "History incomplete; counts below may omit attempts.",
        f"Terminal attempts: {terminal}. File-edit attempts: {edits}.",
        f"Possible GitHub label-write commands: {labels} (command-text matches, attempts only).",
        "Command-text matches are not exhaustive and can include quoted examples. "
        "Shell exit status does not confirm an API write; failed pipelines can still write.",
        "Remote effects: not independently verified by the runner. "
        "Later issue state does not establish who changed it.", "",
    ]
    for number, (start, end) in enumerate(zip(before, after), 1):
        prefix = f"Repository {number}: "
        if not start.get("available") or not end.get("available"):
            lines.append(prefix + "Git comparison unavailable.")
            continue
        changed = [key for key in ("branch", "HEAD", "local branches", "index", "worktree")
                   if start[key] != end[key]]
        lines.append(prefix + ("Changed: " + ", ".join(changed) + "." if changed else
                               "No final Git differences observed."))
        lines.append(f"dirty before={'yes' if start['dirty'] else 'no'}; "
                     f"dirty after={'yes' if end['dirty'] else 'no'}.")
    if not before:
        lines.append("No configured repositories available for Git comparison.")
    lines.extend([
        "", "Git comparisons cover HEAD, local branches, index and non-ignored dirty "
        "files after repository setup. They are point-in-time observations, not proof "
        "of which run owns an edit. Ignored files, submodule worktrees and other Git metadata "
        "are not covered. "
        "Unchanged final state does not erase earlier tool attempts. No cleanup was performed.",
        "Side-effect-free execution is not verified.",
    ])
    return "\n".join(lines)


def publish_report(workspace, conversation, received_events, before, repo_dirs):
    try:
        persisted, complete = read_events(workspace, conversation.id)
        report = build_report(
            [*received_events, *persisted], before,
            snapshot_repositories(workspace, repo_dirs),
            history_complete=complete,
        )
    except Exception:
        report = ("## Runner activity report\n\nGenerated by the OpenHands automation runner. "
                  "Activity verification unavailable; any preceding claim of no writes or "
                  "pre-existing edits is unverified. No cleanup was performed.")
    # This SDK path appends a user turn with run=False, changing FINISHED to IDLE.
    # Never send into active/paused execution, or rewrite the model's answer.
    try:
        if conversation.state.execution_status != ConversationExecutionStatus.FINISHED:
            raise RuntimeError("execution not finished")
        conversation.send_message(report)
    except Exception:
        raise RuntimeError("Runner activity report delivery not confirmed") from None
