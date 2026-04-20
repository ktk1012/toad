from typing import TypedDict, Literal, NotRequired

type Tag = str
"""A tag used for categorizing the agent. For example: 'open-source', 'reasoning'."""
type OS = Literal["macos", "linux", "windows", "*"]
"""An operating system identifier, or a '*" wildcard, if it is the same for all OSes."""
type Action = str
"""An action which the agent supports."""
type AgentType = Literal["coding", "chat"]
"""The type of agent. More types TBD."""
type AgentProtocol = Literal["acp"]
"""The protocol used to communicate with the agent. Currently only "acp" is supported."""
type AgentTransport = Literal["stdio", "websocket"]
"""Transport used to carry the ACP protocol. Defaults to 'stdio'."""
type AgentTrustLevel = Literal["trusted", "untrusted"]
"""How much the client trusts the agent. `trusted` agents run terminal
commands without per-call confirmation; `untrusted` agents must get
explicit user approval for each shell invocation. Defaults depend on
transport: stdio agents are trusted, websocket agents are untrusted."""


class Command(TypedDict):
    """Used to perform an action associate with an Agent."""

    description: str
    """Describes what the script will do. For example: 'Install Claude Code'."""
    command: str
    """Command to run."""
    bootstrap_uv: NotRequired[bool]
    """Bootstrap UV installer (set to `true` if the command users `uv`)."""


class Agent(TypedDict):
    """Describes an agent which Toad can connect to. Currently only Agent Client Protocol is supported.

    This information is stoed within TOML files, where the filename is the "identity" key plus the extension ".toml"

    """

    active: NotRequired[bool]
    """If `True` (default), the agent will be shown in the UI. If `False` the agent will be removed from the UI."""
    recommended: NotRequired[bool]
    """Agent is in recommended set. Set to `True` in main branch only if previously agreed with Will McGugan."""
    identity: str
    """A unique identifier for this agent. Should be a domain the agent developer owns,
    although it doesn't have to resolve to anything. Must be useable in a filename on all platforms. 
    For example: 'claude.anthropic.ai'"""
    name: str
    """The name of the agent. For example: 'Claude Code'."""
    short_name: str
    """A short name, usable on the command line. Try to make it unique. For example: 'claude'."""
    url: str
    """A URL for the agent."""
    protocol: AgentProtocol
    """The protocol used by the agent. Currently only 'acp' is supported."""
    type: "AgentType"
    """The type of the agent. Currently "coding" or "chat". More types TBD."""
    author_name: str
    """The author of the agent. For example 'Anthropic'."""
    author_url: str
    """The authors homepage. For example 'https://www.anthropic.com/'."""
    publisher_name: str
    """The publisher's name (individual or organization that wrote this data)."""
    publisher_url: str
    """The publisher's url."""
    description: str
    """A description of the agent. A few sentences max. May contain content markup (https://textual.textualize.io/guide/content/#markup) if used subtly."""
    tags: list[Tag]
    """Tags which identify the agent. Should be empty for now."""
    help: str
    """A Markdown document with additional details regarding the agent."""
    welcome: NotRequired[str]
    """A Markdown document shown to the user when the conversation starts. Should contain a welcome message and any advice on getting started."""
    run_command: NotRequired[dict[OS, str]]
    """Command to run the agent, by OS or wildcard. Required when transport is 'stdio'."""
    transport: NotRequired[AgentTransport]
    """Transport carrying the ACP protocol. Defaults to 'stdio'."""
    agent_endpoint: NotRequired[str]
    """Endpoint URL to reach the remote agent (e.g. 'wss://agent.example.com/acp').
    Required when transport is 'websocket'. Distinct from `url`, which is the
    agent's homepage. Supports ${VAR} environment-variable substitution."""
    headers: NotRequired[dict[str, str]]
    """Extra HTTP headers sent on the WebSocket handshake (e.g. authentication).
    Values support ${VAR} environment-variable substitution."""
    trust_level: NotRequired[AgentTrustLevel]
    """Explicit trust level for this agent. If omitted, the default is
    derived from `transport`: stdio -> 'trusted', websocket -> 'untrusted'.
    Set explicitly to override (e.g. a self-hosted websocket agent you
    operate yourself can be marked 'trusted')."""
    actions: dict[OS, dict[Action, Command]]
    """Scripts to perform actions, typically at least to install the agent."""
