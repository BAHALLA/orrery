"""Orrery's coordinator: the chat root and the batch triage Workflow.

The package imports nothing at load time. ``orrery_assistant.app`` must be able
to activate learning mode before any agent is built (AEP-032), and uvicorn
imports this package before the module it serves.
"""
