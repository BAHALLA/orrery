def test_every_tool_touching_cross_session_state_is_marked():
    """`user:`/`app:` state outlives the session; the learning sandbox refuses
    such tools by this marker, so a new one must carry it (AEP-032)."""
    import inspect

    from ops_journal_agent import tools
    from orrery_core.security.guardrails import is_cross_session

    touching = {
        name
        for name, fn in inspect.getmembers(tools, inspect.iscoroutinefunction)
        if fn.__module__ == tools.__name__
        and any(prefix in inspect.getsource(fn) for prefix in ('"user:', '"app:', "_KEY"))
    }
    assert touching, "the scan found nothing: it no longer matches the module"
    unmarked = {name for name in touching if not is_cross_session(getattr(tools, name))}
    assert not unmarked, unmarked
