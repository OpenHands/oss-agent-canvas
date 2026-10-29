import ast

PRIORITY = {
    "experienced_user_harm": "medium_or_high",
    "actual_product_reproduction": "medium_or_high",
    "synthetic_only": "low",
    "no_qualifying_evidence": "low",
    "insufficient": "unscorable",
}
READINESS = {
    "observable_complete_scope": "ready",
    "unresolved_product_decision": "not_ready",
    "unmet_author_requirement": "not_ready",
    "unknown": "unspecified",
}


def baseline_policy(source):
    policies = []
    for path, text in source["files"].items():
        if not path.endswith(".py"):
            continue
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.ClassDef) and node.name == "IssueTriage":
                policies.extend(
                    ast.get_source_segment(text, method)
                    for method in node.body
                    if isinstance(method, ast.FunctionDef) and method.name == "_prompt"
                )
    if len(policies) != 1:
        raise ValueError("Historical annotation requires one issue-triage policy")
    return policies[0]


def text_values(value):
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for item in value.values() for text in text_values(item)]
    if isinstance(value, list):
        return [text for item in value for text in text_values(item)]
    return []


def outcome(result, native):
    original = "\n\n".join(text_values(native["task"]))
    for field, options, optional in (
        ("priority", PRIORITY, {"no_qualifying_evidence", "insufficient"}),
        ("readiness", READINESS, {"unknown"}),
    ):
        basis, quote = result[field + "_basis"], result[field + "_quote"]
        if (
            basis not in options
            or not isinstance(quote, str)
            or len(quote) > 2000
            or (quote and quote not in original)
            or (not quote and basis not in optional)
        ):
            raise ValueError(f"Expected {field} outcome lacks grounded task evidence")
    rationale = result["rationale"]
    if not isinstance(rationale, str) or not 1 <= len(rationale) <= 2000:
        raise ValueError("Invalid expected-outcome rationale")
    return {
        "priority": PRIORITY[result["priority_basis"]],
        "readiness": READINESS[result["readiness_basis"]],
        **{
            field: result[field]
            for field in (
                "priority_basis",
                "priority_quote",
                "readiness_basis",
                "readiness_quote",
                "rationale",
            )
        },
    }
