"""Helpers to build Google Chat Cards v2 for interactive elements."""

from __future__ import annotations

from typing import Any

from orrery_core import LEVEL_DESTRUCTIVE

# Subsystem keys written to session state by the parallel health-checker
# nodes in the triage Workflow. Order here drives the rendering order in
# progress and result cards.
SUBSYSTEMS: tuple[tuple[str, str], ...] = (
    ("kafka_status", "Kafka"),
    ("k8s_status", "Kubernetes"),
    ("docker_status", "Docker"),
    ("observability_status", "Observability"),
    ("elasticsearch_status", "Elasticsearch"),
)

# Remediation keys written by the remediation subgraph (actor → verifier → summarizer).
REMEDIATION_KEYS: tuple[str, ...] = (
    "remediation_action",
    "verification_result",
    "remediation_summary",
)

# Agent → friendly label for the "Current step" line.
_AGENT_LABELS: dict[str, str] = {
    "orrery_assistant": "Coordinating",
    "incident_triage_agent": "Running incident triage",
    "health_check_agent": "Running parallel health checks",
    "kafka_health_checker": "Checking Kafka",
    "k8s_health_checker": "Checking Kubernetes",
    "docker_health_checker": "Checking Docker",
    "observability_health_checker": "Checking Observability",
    "elasticsearch_health_checker": "Checking Elasticsearch",
    "triage_summarizer": "Synthesizing findings",
    "journal_writer": "Saving to journal",
    "remediation_pipeline": "Remediating",
    "remediation_loop": "Remediation loop",
    "remediation_actor": "Executing remediation step",
    "remediation_verifier": "Verifying remediation",
    "remediation_summarizer": "Summarizing remediation",
}


def build_confirmation_card(
    tool_name: str,
    args: dict[str, Any],
    reason: str,
    level: str,
    action_id: str,
    interactive_buttons: bool = False,
) -> dict[str, Any]:
    """Build a single Google Chat Card v2 entry for a tool confirmation.

    Returns a ``{"cardId", "card"}`` dict suitable for inclusion in a
    ``cardsV2`` array. The handler merges multiple entries into the final
    reply (synchronous webhook response, or a REST-posted message on the
    Pub/Sub transport).

    Two decision affordances, chosen by ``interactive_buttons``:

    * ``False`` (default, Pub/Sub-safe) — the card asks the operator to
      **reply** ``approve`` / ``deny`` in the card's thread. Replies are plain
      MESSAGE events, which the Pub/Sub transport always delivers with the
      thread attached; the handler resolves them against the thread's pending.
    * ``True`` (HTTP endpoint only) — real Approve/Deny **buttons** whose
      ``CARD_CLICKED`` event carries the exact ``action_id``. Do not enable on
      Pub/Sub: Google's add-ons runtime resolves a click with a synchronous
      HTTPS round-trip, which a pull transport cannot answer — the click fails
      on Google's side with "the Chat app didn't respond" (error code 3).
    """
    emoji = "⚠️" if level == LEVEL_DESTRUCTIVE else "\U0001f535"
    level_label = "DESTRUCTIVE" if level == LEVEL_DESTRUCTIVE else "Confirmation Required"

    header_text = f"{emoji} {level_label}: {tool_name}"
    args_text = ", ".join(f"<i>{k}={v}</i>" for k, v in args.items()) if args else "<i>none</i>"

    widgets: list[dict[str, Any]] = []
    if reason:
        widgets.append({"textParagraph": {"text": f"<b>Reason:</b> {reason}"}})
    widgets.append({"textParagraph": {"text": f"<b>Arguments:</b> {args_text}"}})
    if interactive_buttons:
        widgets.append(
            {
                "buttonList": {
                    "buttons": [
                        {
                            "text": "✅ Approve",
                            "onClick": {
                                "action": {
                                    "function": "confirm_action",
                                    "parameters": [{"key": "action_id", "value": action_id}],
                                }
                            },
                        },
                        {
                            "text": "❌ Deny",
                            "onClick": {
                                "action": {
                                    "function": "deny_action",
                                    "parameters": [{"key": "action_id", "value": action_id}],
                                }
                            },
                        },
                    ]
                }
            }
        )
    else:
        widgets.append(
            {
                "textParagraph": {
                    "text": (
                        "👉 Reply <b>approve</b> in this thread to proceed, "
                        "or <b>deny</b> to cancel."
                    )
                }
            }
        )
    # Requester-only approval is enforced server-side; the hint keeps a
    # teammate's refused decision from being a surprise.
    widgets.append(
        {"textParagraph": {"text": "<i>Only the requester can approve. Anyone can deny.</i>"}}
    )

    return {
        "cardId": action_id,
        "card": {
            "header": {"title": header_text, "subtitle": "Safety Guardrail"},
            "sections": [{"widgets": widgets}],
        },
    }


def classify_status(text: str | None) -> str:
    """Infer a coarse severity (ok/warn/fail) from a status string.

    The health-check sub-agents emit freeform LLM prose, so exact
    structure is not guaranteed. Match on signal words that are stable
    across agents; default to ``ok`` when nothing severe is found.
    """
    if not text:
        return "pending"
    lowered = text.lower()
    fail_tokens = (
        "red",
        "critical",
        "failing",
        "failed",
        " down",
        "unavailable",
        "unassigned shard",
        "crashloop",
        "oomkilled",
    )
    if any(tok in lowered for tok in fail_tokens):
        return "fail"
    warn_tokens = (
        "yellow",
        "warning",
        "warn",
        "degraded",
        "lag",
        "pending",
        "restarts",
    )
    if any(tok in lowered for tok in warn_tokens):
        return "warn"
    return "ok"


def _status_icon(status: str) -> str:
    return {
        "ok": "✅",  # ✅
        "warn": "⚠️",  # ⚠️
        "fail": "❌",  # ❌
        "pending": "⏳",  # ⏳
        "running": "\U0001f504",  # 🔄
    }.get(status, "⏳")


def _overall_status(chips: dict[str, dict[str, str]]) -> str:
    """Derive overall ok/warn/fail from subsystem chips."""
    if not chips:
        return "pending"
    severities = {c["status"] for c in chips.values()}
    if "fail" in severities:
        return "fail"
    if "warn" in severities:
        return "warn"
    if severities == {"ok"}:
        return "ok"
    return "running"


def _agent_label(agent: str | None) -> str:
    if not agent:
        return "Working"
    return _AGENT_LABELS.get(agent, agent)


def _first_line(text: str, max_chars: int = 180) -> str:
    """Return the first non-empty line, truncated for card display."""
    if not text:
        return ""
    for raw in text.splitlines():
        line = raw.strip()
        if line:
            return line if len(line) <= max_chars else line[: max_chars - 1] + "…"
    return ""


def build_progress_card(
    *,
    current_agent: str | None,
    current_tool: str | None,
    subsystem_chips: dict[str, dict[str, str]],
    remediation: dict[str, str] | None = None,
    elapsed_seconds: float = 0.0,
    specialists: list[str] | None = None,
) -> dict[str, Any]:
    """Build the live "Investigating…" card.

    Args:
        current_agent: Name of the sub-agent currently executing.
        current_tool: Name of the most recent tool call, if any.
        subsystem_chips: Mapping of state_delta key → ``{"label", "status",
            "summary"}`` describing each health-check subsystem that has
            reported in.
        remediation: Optional mapping of remediation_key → short summary
            for the remediation-loop panel. When None, no panel is shown.
        elapsed_seconds: Wall-clock time since the run started. Rendered
            in the footer to signal forward progress.
        specialists: Pre-rendered lines, one per specialist consulted with its
            tool calls (from the ProgressPlugin feed, so it includes work done
            inside specialists). Omitted when None or empty.
    """
    header_text = "\U0001f50d Investigating…"

    widgets: list[dict[str, Any]] = []

    step_parts = [f"<b>Step:</b> {_agent_label(current_agent)}"]
    if current_tool:
        step_parts.append(f"<b>Tool:</b> <i>{current_tool}</i>")
    widgets.append({"textParagraph": {"text": " • ".join(step_parts)}})

    if specialists:
        widgets.append({"divider": {}})
        widgets.append({"textParagraph": {"text": "<br>".join(specialists)}})

    if subsystem_chips:
        chip_lines: list[str] = []
        for key, label in SUBSYSTEMS:
            chip = subsystem_chips.get(key)
            if not chip:
                continue
            icon = _status_icon(chip["status"])
            summary = chip.get("summary", "")
            if summary:
                chip_lines.append(f"{icon} <b>{label}</b> — {summary}")
            else:
                chip_lines.append(f"{icon} <b>{label}</b>")
        if chip_lines:
            widgets.append(
                {"divider": {}},
            )
            widgets.append(
                {"textParagraph": {"text": "<br>".join(chip_lines)}},
            )

    if remediation:
        widgets.append({"divider": {}})
        rem_lines = ["<b>Remediation</b>"]
        for key in REMEDIATION_KEYS:
            summary = remediation.get(key)
            if not summary:
                continue
            label = key.replace("_", " ").title()
            rem_lines.append(f"\U0001f504 {label}: {summary}")
        widgets.append({"textParagraph": {"text": "<br>".join(rem_lines)}})

    if elapsed_seconds > 0:
        widgets.append(
            {
                "textParagraph": {
                    "text": f"<i>Elapsed {int(elapsed_seconds)}s</i>",
                }
            }
        )

    return {
        "cardId": "progress",
        "card": {
            "header": {"title": header_text, "subtitle": "Orrery Assistant"},
            "sections": [{"widgets": widgets}],
        },
    }


def build_triage_result_card(
    *,
    subsystem_chips: dict[str, dict[str, str]],
    triage_report: str | None,
    user_role: str,
    interactive_buttons: bool = False,
) -> dict[str, Any]:
    """Render the final incident triage report as a structured card.

    Offers remediation when overall status is not healthy and the user has
    operator+ permissions — as a clickable button when ``interactive_buttons``
    (HTTP endpoint only; see :func:`build_confirmation_card`), else as a
    reply-in-thread instruction that rides the Pub/Sub-safe MESSAGE path.
    """
    overall = _overall_status(subsystem_chips)
    overall_icon = _status_icon(overall)
    overall_label = {
        "ok": "All systems healthy",
        "warn": "Degraded",
        "fail": "Critical",
        "running": "In progress",
        "pending": "Pending",
    }.get(overall, "Pending")

    sections: list[dict[str, Any]] = []

    # Subsystem cards — one widget per system that reported.
    chip_widgets: list[dict[str, Any]] = []
    for key, label in SUBSYSTEMS:
        chip = subsystem_chips.get(key)
        if not chip:
            continue
        icon = _status_icon(chip["status"])
        summary = chip.get("summary") or "(no details)"
        chip_widgets.append(
            {
                "textParagraph": {
                    "text": f"{icon} <b>{label}</b><br>{summary}",
                }
            }
        )
    if chip_widgets:
        sections.append({"header": "Subsystems", "widgets": chip_widgets})

    # Summary section — the LLM-synthesized triage report.
    if triage_report:
        sections.append(
            {
                "header": "Summary",
                "widgets": [{"textParagraph": {"text": triage_report}}],
            }
        )

    # Remediation affordance, operator+ only, only when something is wrong.
    # RBAC is still enforced server-side at tool time — this is a UI shortcut.
    if overall in ("warn", "fail") and user_role in ("operator", "admin"):
        if interactive_buttons:
            # CARD_CLICKED (function ``run_remediation``) dispatches the
            # remediation prompt in the same session — HTTP endpoint only.
            remediation_widget: dict[str, Any] = {
                "buttonList": {
                    "buttons": [
                        {
                            "text": "🔧 Run remediation",
                            "onClick": {"action": {"function": "run_remediation"}},
                        }
                    ]
                }
            }
        else:
            # Pub/Sub-safe: a reply is a MESSAGE event the LLM routes to the
            # remediation flow like any other operator request.
            remediation_widget = {
                "textParagraph": {
                    "text": "👉 To remediate this incident, reply <b>remediate</b> in this thread."
                }
            }
        sections.append({"widgets": [remediation_widget]})

    if not sections:
        sections.append(
            {"widgets": [{"textParagraph": {"text": triage_report or "(no response)"}}]}
        )

    return {
        "cardId": "triage_result",
        "card": {
            "header": {
                "title": f"{overall_icon} Triage Report",
                "subtitle": overall_label,
            },
            "sections": sections,
        },
    }


def build_error_card(message: str) -> dict[str, Any]:
    """Small card used when a background run fails."""
    return {
        "cardId": "error",
        "card": {
            "header": {"title": "❌ Error", "subtitle": "Orrery Assistant"},
            "sections": [{"widgets": [{"textParagraph": {"text": message}}]}],
        },
    }
