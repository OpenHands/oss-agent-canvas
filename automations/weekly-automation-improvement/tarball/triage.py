import copy
import hashlib
from collections import Counter

from common import encode, timestamp, topic_id, write_json
from triage_agent import rank
from laminar import object_value
from main import TEXT, object_schema

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
            conversation = record["owner"]["conversation_id"]
            if conversation in conversations:
                continue
            conversations.add(conversation)
            payload = object_value(record["event"]["payload"])
            examples.append(
                {
                    "event_id": record["event"]["id"],
                    "conversation_id": conversation,
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
                    {item["owner"]["conversation_id"] for item in members}
                ),
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
    grouped = {}
    for record in records:
        grouped.setdefault(record["owner"]["automation_id"], []).append(record)
    conversations = {
        identifier: "c" + str(i)
        for i, identifier in enumerate(
            sorted({r["owner"]["conversation_id"] for r in records}), 1
        )
    }
    automations, cards, event_groups = {}, [], {}
    for i, (automation, rows) in enumerate(sorted(grouped.items()), 1):
        reference = "a" + str(i)
        automations[automation] = {
            "ref": reference,
            "name": rows[0]["owner"]["automation_name"],
            "events": len(rows),
        }
        groups, membership = group_findings(rows)
        for card in groups:
            card = card | {"automation_id": automation, "automation_ref": reference}
            for record in membership[card["group_id"]]:
                event_groups[record["event"]["id"]] = card["group_id"]
            card["examples"] = [
                {
                    key: value
                    for key, value in example.items()
                    if key not in ("event_id", "conversation_id")
                }
                | {
                    "event_ref": event_refs[example["event_id"]],
                    "conversation_ref": conversations[example["conversation_id"]],
                }
                for example in card["examples"]
            ]
            cards.append(card)
    references = {
        "events": events,
        "conversations": {v: k for k, v in conversations.items()},
        "event_groups": event_groups,
    }
    context = {
        "total_events": len(records),
        "affected_conversations": len(conversations),
        "automations": automations,
        "groups": cards,
        "event_catalog": [
            {
                "event_ref": reference,
                "automation_ref": automations[
                    by_event[identifier]["owner"]["automation_id"]
                ]["ref"],
                "conversation_ref": conversations[
                    by_event[identifier]["owner"]["conversation_id"]
                ],
            }
            for reference, identifier in events.items()
        ],
        "existing_improvement_prs": prs,
        "previous_attempts": list(previous_attempts),
        "explicit_retry_topics": [
            t for t in previous_attempts if t["id"] in retry_topic_ids
        ],
    }
    prepared_records = [
        record | {"event_ref": event_refs[record["event"]["id"]]} for record in records
    ]
    return context, references, prepared_records


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
        automation_ids = {record["owner"]["automation_id"] for record in chosen}
        if len(automation_ids) != 1:
            raise ValueError("A repair topic must belong to one automation")
        automation_id = next(iter(automation_ids))
        identifier = topic_id(automation_id, topic["key"])
        if identifier in seen_topics:
            raise ValueError("Triage repeats a topic key")
        if len({record["owner"]["conversation_id"] for record in chosen}) != len(
            chosen
        ):
            raise ValueError("Topic representatives repeat a conversation")
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
