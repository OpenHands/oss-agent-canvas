import copy
import hashlib
from collections import Counter

from common import encode, timestamp, topic_id, write_json
from attribution import automation_ids, candidates, conversation_ids, unit
from triage_agent import rank
from laminar import object_value

TEXT = {"type": "string"}


def object_schema(properties):
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


TRIAGE = object_schema(
    {
        "topics": {
            "type": "array",
            "items": object_schema(
                {
                    "key": {
                        "type": "string",
                        "maxLength": 40,
                        "pattern": "^[a-z0-9]+(?:-[a-z0-9]+)*$",
                    },
                    "title": TEXT,
                    "representative_event_refs": {
                        "type": "array",
                        "items": TEXT,
                        "minItems": 1,
                        "maxItems": 3,
                    },
                    "reason": TEXT,
                    "priority": {"type": "integer", "minimum": 0, "maximum": 3},
                }
            ),
        },
        "no_action_reason": TEXT,
    }
)


def definition_key(definition):
    return hashlib.sha256(
        encode(
            {
                key: definition.get(key)
                for key in (
                    "id",
                    "projectId",
                    "prompt",
                    "structuredOutput",
                    "model",
                    "llmProfileId",
                )
            }
        ).encode()
    ).hexdigest()


def group_findings(records):
    groups = {}
    for record in records:
        event = record["event"]
        payload = object_value(event["payload"])
        clusters = event.get("leaf_clusters") or []
        signature = str(payload.get("signature") or "").strip().lower() or event["id"]
        key = (
            "cluster:" + str(sorted(clusters)[0])
            if clusters
            else "signature:"
            + encode(
                [
                    str(payload.get("cause", "unknown")),
                    signature,
                ]
            )
        )
        groups.setdefault(key, []).append(record)
    cards, membership = [], {}
    for key, members in sorted(groups.items()):
        identifier = hashlib.sha256(key.encode()).hexdigest()[:16]
        membership[identifier] = members
        members = sorted(
            members,
            key=lambda item: (
                -int(item["event"].get("severity") or 0),
                -timestamp(item["run"]["updated_at"]).timestamp(),
                item["event"]["id"],
            ),
        )
        examples, conversations = [], set()
        for record in members:
            identity = unit(record)
            if identity in conversations:
                continue
            conversations.add(identity)
            payload = object_value(record["event"]["payload"])
            examples.append(
                {
                    "event_id": record["event"]["id"],
                    "conversation_id": record["owner"]["conversation_id"]
                    if record.get("owner")
                    else None,
                    "conversation_ids": conversation_ids(record),
                    "trace_id": record["event"]["trace_id"],
                    "attribution_status": record.get("attribution_status", "resolved"),
                    "severity": record["event"].get("severity"),
                    "finding": {
                        field: str(payload.get(field, ""))[:1200]
                        for field in (
                            "summary",
                            "cause",
                            "outcome",
                            "recommendation",
                            "evidence",
                        )
                    },
                }
            )
            if len(examples) == 3:
                break
        cards.append(
            {
                "group_id": identifier,
                "summary": examples[0]["finding"]["summary"],
                "grouping": "laminar_leaf_cluster"
                if key.startswith("cluster:")
                else "exact_signature",
                "events": len(members),
                "affected_conversations": len(
                    {
                        item["owner"]["conversation_id"]
                        for item in members
                        if item.get("owner")
                    }
                ),
                "candidate_conversations": len(
                    {c for item in members for c in conversation_ids(item)}
                ),
                "affected_traces": len({item["event"]["trace_id"] for item in members}),
                "outcomes": dict(
                    Counter(
                        str(
                            object_value(item["event"]["payload"]).get(
                                "outcome", "unknown"
                            )
                        )
                        for item in members
                    )
                ),
                "causes": dict(
                    Counter(
                        str(
                            object_value(item["event"]["payload"]).get(
                                "cause", "unknown"
                            )
                        )
                        for item in members
                    )
                ),
                "latest": max(item["run"]["updated_at"] for item in members),
                "examples": examples,
                "examples_are_excerpts": True,
            }
        )
    return cards, membership


def prepare(records, prs, previous_attempts, retry_topic_ids=()):
    by_event = {record["event"]["id"]: record for record in records}
    events = {
        "e" + str(i): identifier for i, identifier in enumerate(sorted(by_event), 1)
    }
    event_refs = {identifier: reference for reference, identifier in events.items()}
    conversations = {
        identifier: "c" + str(i)
        for i, identifier in enumerate(
            sorted({c for r in records for c in conversation_ids(r)}), 1
        )
    }
    traces = {
        identifier: "t" + str(i)
        for i, identifier in enumerate(
            sorted({r["event"]["trace_id"] for r in records}), 1
        )
    }
    known_automations = sorted({a for r in records for a in automation_ids(r)})
    automations = {}
    for i, identifier in enumerate(known_automations, 1):
        rows = [r for r in records if identifier in automation_ids(r)]
        names = [
            o.get("automation_name", "")
            for r in rows
            for o in candidates(r)
            if o.get("automation_id") == identifier
        ]
        automations[identifier] = {
            "ref": "a" + str(i),
            "name": next((n for n in names if n), ""),
            "events": len(rows),
        }
    grouped = {}
    for record in records:
        grouped.setdefault(tuple(automation_ids(record)), []).append(record)
    cards, event_groups = [], {}
    for owners, rows in sorted(grouped.items()):
        groups, membership = group_findings(rows)
        for card in groups:
            card |= {
                "automation_id": owners[0] if len(owners) == 1 else None,
                "automation_ref": automations[owners[0]]["ref"]
                if len(owners) == 1
                else None,
                "automation_ids": list(owners),
                "automation_refs": [automations[a]["ref"] for a in owners],
            }
            for record in membership[card["group_id"]]:
                event_groups[record["event"]["id"]] = card["group_id"]
            card["examples"] = [
                {
                    k: v
                    for k, v in example.items()
                    if k
                    not in (
                        "event_id",
                        "conversation_id",
                        "conversation_ids",
                        "trace_id",
                    )
                }
                | {
                    "event_ref": event_refs[example["event_id"]],
                    "conversation_ref": conversations.get(example["conversation_id"]),
                    "conversation_refs": [
                        conversations[c] for c in example["conversation_ids"]
                    ],
                    "trace_ref": traces[example["trace_id"]],
                }
                for example in card["examples"]
            ]
            cards.append(card)
    references = {
        "events": events,
        "conversations": {v: k for k, v in conversations.items()},
        "traces": {v: k for k, v in traces.items()},
        "event_groups": event_groups,
    }
    context = {
        "total_events": len(records),
        "affected_conversations": len(
            {r["owner"]["conversation_id"] for r in records if r.get("owner")}
        ),
        "candidate_conversations": len(conversations),
        "affected_traces": len(traces),
        "unresolved_attribution_events": sum(not r.get("owner") for r in records),
        "automations": automations,
        "groups": cards,
        "event_catalog": [
            {
                "event_ref": reference,
                "automation_ref": automations[owners[0]]["ref"]
                if len(owners := automation_ids(by_event[identifier])) == 1
                else None,
                "automation_refs": [automations[a]["ref"] for a in owners],
                "conversation_ref": conversations.get(
                    by_event[identifier]["owner"]["conversation_id"]
                )
                if by_event[identifier].get("owner")
                else None,
                "conversation_refs": [
                    conversations[c] for c in conversation_ids(by_event[identifier])
                ],
                "trace_ref": traces[by_event[identifier]["event"]["trace_id"]],
                "attribution_status": by_event[identifier].get(
                    "attribution_status", "resolved"
                ),
            }
            for reference, identifier in events.items()
        ],
        "existing_improvement_prs": prs,
        "previous_attempts": list(previous_attempts),
        "explicit_retry_topics": [
            t for t in previous_attempts if t["id"] in retry_topic_ids
        ],
    }
    return (
        context,
        references,
        [r | {"event_ref": event_refs[r["event"]["id"]]} for r in records],
    )


def validate(result, records, references, limit):
    if (
        not isinstance(result, dict)
        or set(result) != {"topics", "no_action_reason"}
        or not isinstance(result["topics"], list)
    ):
        raise ValueError("Triage must return a finite topic list and no-action reason")
    if not result["topics"] and not str(result["no_action_reason"]).strip():
        raise ValueError("Triage must explain an empty plan")
    by_id = {record["event"]["id"]: record for record in records}
    events = references["events"]
    seen_events, seen_topics, plans = set(), set(), []
    for position, topic in enumerate(result["topics"]):
        if not isinstance(topic, dict) or set(topic) != {
            "key",
            "title",
            "priority",
            "reason",
            "representative_event_refs",
        }:
            raise ValueError("Invalid topic fields")
        if type(topic["priority"]) is not int or not 0 <= topic["priority"] <= 3:
            raise ValueError("Topic priority must be an integer from P0 through P3")
        for field, bound in (("title", 200), ("reason", 4000)):
            if (
                not isinstance(topic[field], str)
                or not 1 <= len(topic[field].strip()) <= bound
            ):
                raise ValueError("Topic needs a bounded title and explanation")
        refs = topic["representative_event_refs"]
        if (
            not isinstance(refs, list)
            or not 1 <= len(refs) <= limit
            or any(not isinstance(ref, str) for ref in refs)
            or len(set(refs)) != len(refs)
            or not set(refs) <= events.keys()
        ):
            raise ValueError("Topic cites duplicate or foreign representative events")
        selected = [events[reference] for reference in refs]
        if set(selected).intersection(seen_events):
            raise ValueError("Topic repeats evidence from another topic")
        chosen = [by_id[event_id] for event_id in selected]
        owners = sorted({a for record in chosen for a in automation_ids(record)})
        automation_id = owners[0] if len(owners) == 1 else None
        identifier = topic_id("shared", topic["key"])
        if identifier in seen_topics:
            raise ValueError("Triage repeats a topic key")
        used_conversations, used_traces = set(), set()
        for record in chosen:
            conversations = set(conversation_ids(record))
            trace = record["event"]["trace_id"]
            if trace in used_traces or conversations.intersection(used_conversations):
                raise ValueError("Topic representatives repeat a conversation or trace")
            used_conversations.update(conversations)
            used_traces.add(trace)
        seen_events.update(selected)
        seen_topics.add(identifier)
        plans.append(
            {
                key: value
                for key, value in topic.items()
                if key != "representative_event_refs"
            }
            | {
                "id": identifier,
                "automation_id": automation_id,
                "automation_ids": owners,
                "selection_order": position,
                "group_ids": sorted(
                    {references["event_groups"][event_id] for event_id in selected}
                ),
                "representative_event_ids": selected,
            }
        )
    return sorted(
        plans, key=lambda topic: (topic["priority"], topic["selection_order"])
    )


def triage(args, records, prs, completed_topics=()):
    context, references, prepared_records = prepare(
        records, prs, completed_topics, getattr(args, "retry_topic_ids", [])
    )
    write_json(args.output / "triage-input.json", context)
    write_json(args.output / "triage-references.json", references)
    schema = copy.deepcopy(TRIAGE)
    schema["properties"]["topics"]["items"]["properties"]["representative_event_refs"][
        "maxItems"
    ] = args.limit
    result, measurement, issue = rank(args, context, prepared_records, schema)
    plans = validate(result, records, references, args.limit)
    issue.record(
        "Attempts and decisions — validated priority queue",
        f"Validated {len(plans)} focused repair topics, ordered globally by priority. These are proposed investigations, not confirmed defects or proven repairs.",
        {"ranked-topics.json": plans, "triage-references.json": references},
    )
    aggregation = context | {
        "luna_triage": {
            "topics": plans,
            "no_action_reason": result["no_action_reason"],
        },
        "triage_measurement": measurement,
        "triage_issue": issue.refresh(),
    }
    write_json(args.output / "triage-result.json", aggregation)
    return plans, aggregation
