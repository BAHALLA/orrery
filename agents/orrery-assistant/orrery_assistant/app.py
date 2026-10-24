from orrery_assistant.admin import ChatAgentAdmin
from orrery_assistant.agent import _specialists, root_agent
from orrery_assistant.probes import default_probes
from orrery_core import create_memory_service, default_plugins
from orrery_core.agent.declarative import tool_catalogue
from orrery_core.serving.runner import create_context_cache_config
from orrery_core.serving.server import ServerConfig, create_app

config = ServerConfig.from_env()
api = create_app(
    root_agent=root_agent,
    app_name="orrery",
    plugins=default_plugins(enable_auth=config.auth_enabled, enable_memory=True),
    config=config,
    # Persist long-term memory in the same database as sessions when configured
    # (via DATABASE_URL); falls back to in-memory recall otherwise.
    memory_service=create_memory_service(db_url=config.database_url),
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
    studio_catalogue=lambda: tool_catalogue(_specialists)[0],
)
