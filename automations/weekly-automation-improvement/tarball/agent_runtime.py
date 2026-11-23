import json
import os
from types import SimpleNamespace
from urllib.parse import quote


class AgentDeadline(BaseException):
    pass


def create_agent(model, name, settings, root):
    from pydantic import SecretStr
    from openhands.sdk import LLM
    from openhands.sdk.tool import Tool
    from openhands.tools.preset.default import (
        get_default_agent,
        register_builtins_agents,
    )
    from openhands.tools.task import TaskToolSet

    if name:
        from openhands.sdk.profiles import resolve_agent_profile, validate_agent_profile
        from openhands.sdk.skills import load_available_skills
        from openhands.sdk.workspace import RemoteWorkspace
        from openhands.tools import register_default_tools
        from sources import native_request

        host = os.environ.get("AGENT_SERVER_URL", "")
        profile = validate_agent_profile(
            json.loads(
                native_request(host, "/api/agent-profiles/" + quote(name, safe=""))
            )["profile"]
        )
        if profile.agent_kind != "openhands":
            raise ValueError("The agent profile must use an OpenHands agent")

        def load_llm(reference, *, cipher=None):
            value = json.loads(
                native_request(
                    host,
                    "/api/profiles/" + quote(reference, safe=""),
                    expose_secrets=True,
                )
            )["config"]
            try:
                return LLM.model_validate(value).model_copy(
                    update={
                        "log_completions": True,
                        "log_completions_folder": str(root / "completions"),
                    }
                )
            except ValueError:
                raise ValueError(
                    "The agent profile's LLM configuration is invalid"
                ) from None

        native_settings = json.loads(
            native_request(host, "/api/settings", expose_secrets=True)
        )["agent_settings"]
        public = (
            load_available_skills(include_public=True)
            if getattr(profile, "public_skills", None) != []
            else {}
        )
        user = load_available_skills(include_user=True)
        skills = public | user
        critic_key = native_settings.get("verification", {}).get("critic_api_key")
        resolved = resolve_agent_profile(
            profile,
            llm_store=SimpleNamespace(load=load_llm),
            mcp_config=native_settings.get("mcp_config", {}),
            available_skills=list(skills.values()),
            public_skill_names=set(public) - set(user),
            **({"critic_api_key": SecretStr(critic_key)} if critic_key else {}),
        )
        register_default_tools()
        register_builtins_agents()
        agent = resolved.create_agent()
        try:
            with RemoteWorkspace(
                host=host,
                api_key=os.environ.get("OH_SESSION_API_KEYS_0")
                or os.environ.get("SESSION_API_KEY"),
                working_dir=str(root),
            ) as workspace:
                secrets = workspace.get_secrets(agent_profile_id=str(profile.id))
        except ValueError:
            raise ValueError("Could not load the agent profile's secrets") from None
        return (
            agent,
            {
                "name": profile.name,
                "id": str(profile.id),
                "revision": profile.revision,
                "llm_profile_ref": profile.llm_profile_ref,
                "model": resolved.llm.model,
            },
            secrets,
        )

    llm = LLM(
        model=model,
        base_url=os.environ["LLM_BASE_URL"],
        api_key=SecretStr(
            os.environ.get("LITELLM_API_KEY") or os.environ["LLM_API_KEY"]
        ),
        reasoning_effort=settings.get("effort", "high"),
        max_output_tokens=settings.get("max_output_tokens", 16384),
        api_mode="responses",
        num_retries=settings.get("num_retries", 1),
        timeout=settings.get("llm_timeout", 180),
        log_completions=True,
        log_completions_folder=str(root / "completions"),
    )
    agent = get_default_agent(llm=llm)
    register_builtins_agents()
    if not any(tool.name == TaskToolSet.name for tool in agent.tools):
        agent = agent.model_copy(
            update={"tools": [*agent.tools, Tool(name=TaskToolSet.name)]}
        )
    return agent, {"name": "built-in", "model": model}, {}
