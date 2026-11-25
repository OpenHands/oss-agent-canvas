import os
import sqlite3
import uuid
from contextlib import closing

from common import timestamp


EMPTY_EVENT = str(uuid.UUID(int=0))
IN_PROGRESS = ""


def newest(previous, observed):
    if previous is None or observed[:2] > previous[:2]:
        return observed
    if observed[:2] == previous[:2] and observed[2] != previous[2]:
        if previous[2] == IN_PROGRESS:
            return observed
        if observed[2] == IN_PROGRESS:
            return previous
        # Conflicting terminal observations cannot establish an eligible event.
        return (*previous[:2], None)
    return previous


def latest_events(definition, runs, path=None, *, dry_run=False):
    latest = {}
    for run in runs:
        trace = run["trace_id"]
        event = run.get("event_id")
        if run["status"] in ("PENDING", "PROCESSING"):
            event = IN_PROGRESS
        elif run["status"] != "COMPLETED" or event in ("", EMPTY_EVENT):
            event = None
        observed = (
            timestamp(run["updated_at"]).isoformat(timespec="microseconds"),
            run["run_id"],
            event,
        )
        latest[trace] = newest(latest.get(trace), observed)

    if path is not None and latest:
        merge_history(path, definition, latest, dry_run=dry_run)
    return {trace: observation[2] for trace, observation in latest.items()}


def merge_history(path, definition, latest, *, dry_run):
    if dry_run and (not path.exists() or not path.stat().st_size):
        return
    if not dry_run:
        os.close(os.open(path, os.O_CREAT | os.O_RDWR, 0o600))
    uri = path.resolve().as_uri() + ("?mode=ro" if dry_run else "?mode=rw")
    with closing(sqlite3.connect(uri, uri=True)) as db, db:
        db.execute("BEGIN")
        if not dry_run:
            db.execute(
                "CREATE TABLE IF NOT EXISTS latest (definition TEXT, trace TEXT, "
                "updated TEXT NOT NULL, run TEXT NOT NULL, event TEXT, "
                "PRIMARY KEY (definition, trace)) WITHOUT ROWID"
            )
        changed = []
        for trace, observed in latest.items():
            previous = db.execute(
                "SELECT updated, run, event FROM latest WHERE definition=? AND trace=?",
                (definition, trace),
            ).fetchone()
            latest[trace] = newest(previous, observed)
            if latest[trace] != previous:
                changed.append((definition, trace, *latest[trace]))
        if not dry_run:
            db.executemany("INSERT OR REPLACE INTO latest VALUES (?,?,?,?,?)", changed)
