"""The HTTP front door: FastAPI + the web console, serving the chat root.

The only entry point that may run the learning sandbox (AEP-032), so its first
statement activates it, before any import below builds an agent.
"""

# ruff: noqa: E402
# The imports after activation build the agent tree, so they must follow it.
from orrery_core import learning_gate, load_agent_env

# The flag may live in .env, which agent.py would otherwise load only after
# activation had already found it unset (and every builder then refuses it).
load_agent_env(__file__)
learning_gate.activate_http_server()

from orrery_assistant.admin import ChatAgentAdmin
from orrery_assistant.agent import _specialists, root_agent
from orrery_assistant.probes import default_probes
from orrery_core import create_memory_service, default_plugins
from orrery_core.agent.declarative import tool_catalogue
from orrery_core.memory_switch import long_term_memory_enabled, memory_safe_catalogue
from orrery_core.serving.runner import create_context_cache_config
from orrery_core.serving.server import ServerConfig, create_app

config = ServerConfig.from_env()
# The learning sandbox keeps no long-term memory: recall is keyed by the learner,
# not the scenario, so it would replay an earlier attempt's answers (AEP-032).
# ORRERY_LONG_TERM_MEMORY=false turns it off on any other deployment.
remember = not learning_gate.is_active() and long_term_memory_enabled()
api = create_app(
    root_agent=root_agent,
    app_name="orrery",
    plugins=default_plugins(enable_auth=config.auth_enabled, enable_memory=remember),
    config=config,
    # Persist long-term memory in the same database as sessions when configured
    # (via DATABASE_URL); falls back to in-memory recall otherwise.
    memory_service=create_memory_service(db_url=config.database_url) if remember else None,
    context_cache_config=create_context_cache_config(),
    # Powers the console's first-run self-test: which integrations are actually
    # wired. Core stays agent-agnostic, so the probe list is supplied here, from
    # the specialists the deployment runs (the env set: an admin's catalog
    # switch-off removes a route, not the integration, so it is still probed).
    integration_probes=default_probes(_specialists),
    # Lets admins switch specialists and compose declarative agents from the
    # console (writes need ORRERY_ADMIN_EDITING=true); stored next to sessions.
    agent_admin=ChatAgentAdmin(),
    # What a Studio composition may reference as `catalog:<tool>` (AEP-031):
    # the declarative-agent catalogue; the Studio uses its read-only tools only.
    # Without long-term memory, a tool keeping `user:`/`app:` state is left out
    # too: AgentGateway would refuse the run that used it.
    studio_catalogue=lambda: memory_safe_catalogue(tool_catalogue(_specialists)[0]),
)
