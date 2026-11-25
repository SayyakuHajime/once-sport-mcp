"""Server process state must be restored even when the transport fails."""

import logging

import pytest

from once_sport_mcp import server


@pytest.mark.parametrize("transport_fails", [False, True])
def test_main_restores_logging_after_transport_exit(monkeypatch, transport_fails):
    before = logging.root.manager.disable

    def run(*, transport):
        assert transport == "stdio"
        assert logging.root.manager.disable == logging.CRITICAL
        if transport_fails:
            raise RuntimeError("transport stopped")

    monkeypatch.setattr(server.mcp, "run", run)
    try:
        logging.disable(logging.WARNING)
        if transport_fails:
            with pytest.raises(RuntimeError, match="transport stopped"):
                server.main()
        else:
            server.main()
        assert logging.root.manager.disable == logging.WARNING
    finally:
        logging.disable(before)
