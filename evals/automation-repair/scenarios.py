import reviewer_world
import triage_world

TRIAGE = ("reported_paid_harm", "reported_harm_unknown_amount", "speculative_cost_risk")


def scenario(name):
    return (
        triage_world.scenario(name) if name in TRIAGE else reviewer_world.scenario(name)
    )


def world(case):
    return (
        triage_world.World(case)
        if case.get("family") == "triage"
        else reviewer_world.World(case)
    )


def score(state, **kwargs):
    return (
        triage_world.score(state, **kwargs)
        if state["case"].get("family") == "triage"
        else reviewer_world.score(state, **kwargs)
    )
