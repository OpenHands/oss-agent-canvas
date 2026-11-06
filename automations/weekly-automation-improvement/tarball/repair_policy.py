import re


def resolve(config):
    scope = config.get("repair_scope", "customer_stack")
    if scope not in ("customer_stack", "any_authorized"):
        raise ValueError("repair_scope must be customer_stack or any_authorized")
    repositories = config.get("repositories", {})
    if not isinstance(repositories, dict):
        raise ValueError("repositories must map repository names to checkouts")
    names = [config["repository"], *repositories]
    if any(
        not isinstance(name, str) or not re.fullmatch(r"[\w.-]+/[\w.-]+", name)
        for name in names
    ):
        raise ValueError("Invalid repository slug")
    return {"scope": scope, "repositories": sorted(set(names), key=str.casefold)}


def permits(policy, repository):
    return policy["scope"] == "any_authorized" or repository.casefold() in {
        name.casefold() for name in policy["repositories"]
    }


def instructions(policy):
    if policy["scope"] == "customer_stack":
        boundary = (
            "Repair scope is customer_stack. Create or change issues, comments, PRs and source only in these customer repositories: "
            + ", ".join(policy["repositories"])
            + ". You may read upstream source and evidence. If the root cause is outside this scope, record it in a customer issue; propose an upgrade or safe workaround only when justified. Do not publish an upstream issue or PR or edit upstream source. "
        )
    else:
        boundary = (
            "Repair scope is any_authorized. The repository catalog is starting context, not a destination restriction. "
            "You may propose fixes in other relevant repositories when authorized by this installation's GitHub access. "
            "Do not use unrelated discovered credentials or seek broader access. If access or a checkout is missing, report the exact prerequisite. "
        )
    return boundary + (
        "Choose the most maintainable repair layer: customer automation or SDK integration code, prompts/repository instructions, skills/plugins, tools/MCP configuration, agent/LLM profiles, hooks, context/memory settings, dependencies or runtime setup. "
        "Inspect the configuration actually used by the cited runs and its source of truth; current defaults alone do not prove historical behavior. "
        "For settings stored only in a UI/API, identify the supported application path; an unused configuration file is not a fix. Do not change live settings during repair. "
        "Explain the chosen layer and affected shared profiles/automations in the issue. Preserve secret values privately and use references in Git. "
        "Do not broaden permissions, weaken acceptance criteria or hide Signals to claim improvement. "
    )


def resource_errors(policy, resources):
    return [
        resource["url"] + ": repository outside customer_stack repair scope"
        for resource in resources
        if not permits(policy, resource["repository"])
    ]
