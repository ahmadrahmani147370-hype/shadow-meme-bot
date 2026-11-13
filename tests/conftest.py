"""Tests written before v1.1 use tiny new-launch fixtures (mcap ~$50k) and expect every alert to be pushed.
They run with the pre-v1.1 gates; tests marked `v11` (and test_config) see the real v1.1 defaults."""
import pytest

from shadow import config as shadow_config

LEGACY = {"TRADE_MIN_MCAP_USD": "0", "EVAL_MIN_MCAP_USD": "0", "TRADE_MIN_LIQ_USD": "0", "TRADE_MIN_LIQ_MCAP": "0",
          "TRADE_MIN_AGE_MIN": "0", "COINS_PER_DAY": "0", "TELEGRAM_NOTIFY": "all", "MIN_TRADE_SCORE": "65",
          "SHADOW_TRACK": "false"}
shadow_config.TEST_DEFAULTS = dict(LEGACY)       # module-level Settings built at import time are legacy too


def pytest_configure(config):  # noqa: D103
    config.addinivalue_line("markers", "v11: run with the real v1.1 defaults (no legacy overrides)")


@pytest.fixture(autouse=True)
def _legacy_defaults(request, monkeypatch):
    legacy = request.node.get_closest_marker("v11") is None and "test_config" not in request.node.nodeid
    monkeypatch.setattr(shadow_config, "TEST_DEFAULTS", dict(LEGACY) if legacy else {})
    yield
