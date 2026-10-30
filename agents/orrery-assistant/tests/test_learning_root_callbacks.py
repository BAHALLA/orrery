"""The chat root carries no model callbacks of its own (AEP-032 §4).

The answer guard is a plugin, and ADK runs agent-level model callbacks around
plugins: a root ``before_model_callback`` that answers skips the guard, and an
``after_model_callback`` runs after it and could put text back. None exists
today; this pins that, so adding one is a reviewed decision.
"""

from orrery_assistant.agent import orrery_chat_agent


def test_the_chat_root_has_no_model_callbacks():
    assert not orrery_chat_agent.before_model_callback
    assert not orrery_chat_agent.after_model_callback
