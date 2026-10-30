import uuid


def identifier(value):
    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError):
        return None


def candidates(record):
    if "owner_candidates" in record:
        return record["owner_candidates"]
    return [record["owner"]] if record.get("owner") else []


def automation_ids(record):
    return sorted(
        {
            value
            for row in candidates(record)
            if (value := identifier(row.get("automation_id")))
        }
    )


def conversation_ids(record):
    return sorted(
        {
            value
            for row in candidates(record)
            if (value := identifier(row.get("conversation_id")))
        }
    )


def unit(record):
    owner = record.get("owner")
    return (
        ("conversation", owner["conversation_id"])
        if owner
        else ("trace", record["event"]["trace_id"])
    )


def ownership(rows, incomplete=False):
    fields = ("automation_id", "conversation_id", "run_id")
    unique = {}
    for row in rows:
        row = {
            key: identifier(value) or value if key in fields else value
            for key, value in row.items()
        }
        key = tuple(row.get(field) for field in fields)
        if key not in unique or not unique[key].get("automation_name"):
            unique[key] = row
    rows = list(unique.values())
    valid = len(rows) == 1 and all(identifier(rows[0].get(field)) for field in fields)
    if incomplete:
        status = "incomplete"
    elif not rows:
        status = "missing"
    elif valid:
        status = "resolved"
    else:
        status = "ambiguous" if len(rows) > 1 else "invalid"
    return {
        "owner": rows[0] if status == "resolved" else None,
        "attribution_status": status,
    } | ({"owner_candidates": rows} if status != "resolved" else {})
