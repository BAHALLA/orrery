"""Learning sandbox mode for practice labs (AEP-032). Off by default.

Nothing outside this package imports it unless ``ORRERY_LEARNING_MODE`` is
set and the HTTP server activates it (``orrery_core.learning_gate``);
``core/tests/test_learning_isolation.py`` pins that.

- ``manifest``: the scenario manifest (contract v1) and its reloading store.
- ``config``: the deployment preconditions checked at activation.
- ``guard``: the answer matcher and ``AnswerGuardPlugin``.
- ``hints``: disclosure levels, the hint store and the level instruction line.
- ``coach``: the lab coach ``build_chat_agent()`` adds in an activated process.
- ``runtime``: what an activated process holds.
"""
