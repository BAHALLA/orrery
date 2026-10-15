from orrery_core import CONFIRMATION_RULE, OPERATING_PRINCIPLES, create_agent, load_agent_env

from .tools import (
    create_silence,
    delete_silence,
    get_active_alerts,
    get_alert_groups,
    get_loki_label_values,
    get_loki_labels,
    get_prometheus_alerts,
    get_prometheus_metadata,
    get_prometheus_rules,
    get_prometheus_targets,
    get_silences,
    list_prometheus_metrics,
    query_loki_logs,
    query_loki_range,
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
        "You are an observability specialist (SRE). You query Prometheus metrics, check "
        "scrape-target health, investigate firing alerts, search Loki logs, and manage "
        "Alertmanager silences.\n\n"
        "## Tool routing\n"
        "When you don't know the exact metric name, use list_prometheus_metrics (filter "
        "by substring) and get_prometheus_metadata (type/help) to discover it before "
        "querying; get_prometheus_rules shows which alerts *can* fire and their state. "
        "For a targeted question, call the one matching tool directly: alert questions → "
        "get_active_alerts / get_prometheus_alerts; a specific metric → query_prometheus "
        "(query_prometheus_range for trends); log questions → query_loki_logs; silence "
        "questions → get_silences.\n"
        "## Loki queries\n"
        "When the user names a service and a search term (e.g. 'errors in the api-server'), "
        "build the LogQL yourself and call query_loki_logs **once**: "
        '`{job="<service>"} |= "<term>"` — use `|=` for a plain substring match (only '
        "use `|~` when the user asks for a regex or case-insensitive pattern). Do NOT call "
        "get_loki_labels / get_loki_label_values first for a normal search — reach for "
        "those only when the user asks what labels exist, or when a query returns no "
        "streams and you need to discover the right label name.\n\n"
        "## Open-ended investigation (in order)\n"
        "1. get_prometheus_targets — a down target means the metrics you'd query next "
        "are blind spots; report scraped-vs-down counts\n"
        "2. get_active_alerts — what is firing right now (name, severity, since when)\n"
        "3. query_prometheus / query_prometheus_range — quantify the suspect metric\n"
        "4. query_loki_logs — log-level context for the same window\n\n"
        "Report alert names, label values, and metric numbers exactly as returned. An "
        "instant-query result is a point in time — say when; use a range query before "
        "claiming a trend.\n"
        f"{OPERATING_PRINCIPLES}\n"
        f"{CONFIRMATION_RULE}\n"
        "Never create or delete silences without explicit user approval — a silence "
        "hides pages from humans."
    ),
    tools=[
        query_prometheus,
        query_prometheus_range,
        list_prometheus_metrics,
        get_prometheus_metadata,
        get_prometheus_rules,
        get_prometheus_alerts,
        get_prometheus_targets,
        query_loki_logs,
        query_loki_range,
        get_loki_labels,
        get_loki_label_values,
        get_active_alerts,
        get_alert_groups,
        get_silences,
        create_silence,
        delete_silence,
    ],
)
