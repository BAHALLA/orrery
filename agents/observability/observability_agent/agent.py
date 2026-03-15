from ai_agents_core import (
    audit_logger,
    create_agent,
    graceful_tool_error,
    load_agent_env,
    require_confirmation,
)

from .tools import (
    create_silence,
    delete_silence,
    get_active_alerts,
    get_alert_groups,
    get_loki_label_values,
    get_loki_labels,
    get_prometheus_alerts,
    get_prometheus_targets,
    get_silences,
    query_loki_logs,
    query_prometheus,
    query_prometheus_range,
)

load_agent_env(__file__)

root_agent = create_agent(
    name="observability_agent",
    description=(
        "Specialist for observability stack operations. Use this agent for anything "
        "related to Prometheus metrics and alerts, Loki log queries, and "
        "Alertmanager silence management."
    ),
    instruction=(
        "You are an observability specialist. Use your tools to query Prometheus metrics, "
        "check scrape target health, investigate firing alerts, search logs in Loki, and "
        "manage Alertmanager silences.\n\n"
        "When diagnosing issues:\n"
        "1. Start with get_prometheus_targets to check if all targets are healthy\n"
        "2. Check get_active_alerts for currently firing alerts\n"
        "3. Use query_prometheus for specific metric investigation\n"
        "4. Correlate with query_loki_logs for log-level context\n\n"
        "When a tool returns a 'confirmation_required' status, you MUST ask the user "
        "to confirm before calling the tool again. Never create or delete silences "
        "without explicit user approval."
    ),
    tools=[
        query_prometheus,
        query_prometheus_range,
        get_prometheus_alerts,
        get_prometheus_targets,
        query_loki_logs,
        get_loki_labels,
        get_loki_label_values,
        get_active_alerts,
        get_alert_groups,
        get_silences,
        create_silence,
        delete_silence,
    ],
    before_tool_callback=require_confirmation(),
    after_tool_callback=audit_logger(),
    on_tool_error_callback=graceful_tool_error(),
)
