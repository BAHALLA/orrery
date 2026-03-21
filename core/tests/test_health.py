"""Unit tests for health and readiness probe server."""

from unittest.mock import patch

from ai_agents_core.health import HealthServer


class TestHealthServerChecks:
    def test_no_checks_is_ready(self):
        hs = HealthServer()
        ok, details = hs._run_checks()
        assert ok is True
        assert details == {}

    def test_all_checks_pass(self):
        hs = HealthServer()
        hs.register_check("db", lambda: True)
        hs.register_check("cache", lambda: True)
        ok, details = hs._run_checks()
        assert ok is True
        assert details == {"db": True, "cache": True}

    def test_one_check_fails(self):
        hs = HealthServer()
        hs.register_check("db", lambda: True)
        hs.register_check("cache", lambda: False)
        ok, details = hs._run_checks()
        assert ok is False
        assert details == {"db": True, "cache": False}

    def test_exception_in_check_counts_as_failure(self):
        hs = HealthServer()
        hs.register_check("flaky", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        ok, details = hs._run_checks()
        assert ok is False
        assert details == {"flaky": False}

    def test_register_overwrites_existing_check(self):
        hs = HealthServer()
        hs.register_check("db", lambda: False)
        hs.register_check("db", lambda: True)
        ok, details = hs._run_checks()
        assert ok is True


class TestHealthServerStart:
    @patch("ai_agents_core.health.HTTPServer")
    @patch("ai_agents_core.health.threading.Thread")
    def test_start_creates_server(self, mock_thread_cls, mock_http_cls):
        import ai_agents_core.health as health_mod

        health_mod._server_started = False
        try:
            hs = HealthServer()
            hs.start(port=9999)
            mock_http_cls.assert_called_once()
            assert mock_http_cls.call_args[0][0] == ("0.0.0.0", 9999)
            mock_thread_cls.return_value.start.assert_called_once()
        finally:
            health_mod._server_started = False

    @patch("ai_agents_core.health.HTTPServer")
    @patch("ai_agents_core.health.threading.Thread")
    def test_start_only_once(self, mock_thread_cls, mock_http_cls):
        import ai_agents_core.health as health_mod

        health_mod._server_started = False
        try:
            hs = HealthServer()
            hs.start(port=9998)
            hs.start(port=9998)
            assert mock_http_cls.call_count == 1
        finally:
            health_mod._server_started = False

    @patch.dict("os.environ", {"HEALTH_PORT": "7777"})
    @patch("ai_agents_core.health.HTTPServer")
    @patch("ai_agents_core.health.threading.Thread")
    def test_reads_env_port(self, mock_thread_cls, mock_http_cls):
        import ai_agents_core.health as health_mod

        health_mod._server_started = False
        try:
            hs = HealthServer()
            hs.start()
            assert mock_http_cls.call_args[0][0] == ("0.0.0.0", 7777)
        finally:
            health_mod._server_started = False
