import uuid

from laminar import META, SESSION


ORIGIN_FIELDS = {
    "automation_ids": META + "automation.id",
    "automation_run_ids": META + "automation.run_id",
    "conversation_ids": SESSION,
}
UUID_FIELDS = {*ORIGIN_FIELDS, "trace_ids"}
PAYLOAD_FIELDS = {
    "causes": (
        "cause",
        {"workflow", "configuration", "infrastructure", "provider", "unknown"},
    ),
    "outcomes": ("outcome", {"recovered", "unresolved", "unknown"}),
}
FILTER_FIELDS = UUID_FIELDS | PAYLOAD_FIELDS.keys() | {"time_basis"}


def normalize_trace_filter(value):
    if not isinstance(value, dict) or value.keys() - FILTER_FIELDS:
        raise ValueError(
            "trace_filter must be an object containing only supported fields"
        )
    normalized = {}
    basis = value.get("time_basis", "signal_updated")
    if basis not in ("signal_updated", "trace_started"):
        raise ValueError(
            "trace_filter.time_basis must be signal_updated or trace_started"
        )
    if basis == "trace_started":
        normalized["time_basis"] = basis
    for key in sorted(value.keys() - {"time_basis"}):
        entries = value[key]
        if not isinstance(entries, list) or not 1 <= len(entries) <= 100:
            raise ValueError(f"trace_filter.{key} requires between 1 and 100 values")
        if not all(isinstance(item, str) for item in entries):
            raise ValueError(f"trace_filter.{key} requires string values")
        if key in UUID_FIELDS:
            try:
                entries = [str(uuid.UUID(item)) for item in entries]
            except ValueError:
                raise ValueError(f"trace_filter.{key} requires UUID values") from None
        elif any(item not in PAYLOAD_FIELDS[key][1] for item in entries):
            raise ValueError(f"trace_filter.{key} has an unsupported value")
        normalized[key] = sorted(set(entries))
    return normalized


def run_predicates(filters, since, until, signal_scope):
    def window(column):
        lower, upper = (
            t.strftime("toDateTime64('%Y-%m-%d %H:%M:%S.%f',6,'UTC')")
            for t in (since, until)
        )
        return f"{column} >= {lower} AND {column} < {upper}"

    def in_list(column, identifiers):
        values = ",".join(f"'{value}'" for value in identifiers)
        return f"{column} IN ({values})"

    predicates = [signal_scope]
    trace_ids = filters.get("trace_ids", [])
    if trace_ids:
        predicates.append(in_list("trace_id", trace_ids))
    origins = [
        in_list(f"JSONExtractString(attributes,'{attribute}')", filters[key])
        for key, attribute in ORIGIN_FIELDS.items()
        if key in filters
    ]
    if filters.get("time_basis") == "trace_started":
        trace_scope = window("start_time")
        if trace_ids:
            trace_scope += " AND " + in_list("id", trace_ids)
        in_window = f"trace_id IN (SELECT id FROM traces WHERE {trace_scope})"
        if origins:
            origins.append(in_window)
        else:
            predicates.append(in_window)
    else:
        predicates.append(window("updated_at"))
        if origins and not trace_ids:
            run_scope = " AND ".join(predicates)
            origins.append(
                f"trace_id IN (SELECT trace_id FROM signal_runs WHERE {run_scope})"
            )
    if origins:
        if trace_ids:
            origins.append(in_list("trace_id", trace_ids))
        predicates.append(
            "trace_id IN (SELECT trace_id FROM spans WHERE name='automation.conversation' AND "
            + " AND ".join(origins)
            + ")"
        )
    return " AND ".join(predicates)


def matches_payload(filters, payload):
    return all(
        payload.get(field) in filters[key]
        for key, (field, _) in PAYLOAD_FIELDS.items()
        if key in filters
    )
