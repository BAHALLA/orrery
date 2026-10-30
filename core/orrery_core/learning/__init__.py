"""Learning sandbox mode for practice labs (AEP-032). Off by default.

Nothing outside this package imports it unless ``ORRERY_LEARNING_MODE`` is
set; ``core/tests/test_learning_isolation.py`` pins that. Phase 0 ships the
scenario manifest loader only: nothing is wired in yet.
"""
