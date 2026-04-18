from importlib.resources import files
import asyncio

from toad.agent_schema import Agent
from toad import paths


class AgentReadError(Exception):
    """Problem reading the agents."""


async def read_agents() -> dict[str, Agent]:
    """Read agent information.

    Built-in agents are loaded from the packaged `toad.data.agents` resource,
    then user-defined agents under `$XDG_CONFIG_HOME/toad/agents/*.toml` are
    merged on top. A user agent whose identity matches a built-in replaces it.

    Raises:
        AgentReadError: If the files could not be read.

    Returns:
        A mapping of identity on to Agent dict.
    """
    import tomllib

    def read_agents() -> list[Agent]:
        agents: list[Agent] = []
        try:
            for file in files("toad.data").joinpath("agents").iterdir():
                agent: Agent = tomllib.load(file.open("rb"))
                if agent.get("active", True):
                    agents.append(agent)

            user_agents_dir = paths.get_config() / "agents"
            if user_agents_dir.is_dir():
                for user_file in sorted(user_agents_dir.glob("*.toml")):
                    with user_file.open("rb") as fp:
                        user_agent: Agent = tomllib.load(fp)
                    if user_agent.get("active", True):
                        agents.append(user_agent)

        except Exception as error:
            raise AgentReadError(f"Failed to read agents; {error}")

        return agents

    agents = await asyncio.to_thread(read_agents)
    agent_map: dict[str, Agent] = {}
    for agent in agents:
        agent_map[agent["identity"]] = agent

    return agent_map
