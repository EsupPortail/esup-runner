"""Regression checks for deferred, idempotent Runner shutdown on signals."""

import signal

import pytest

from app.managers import process_manager


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_shutdown_signal_unwinds_logging_before_cleanup(monkeypatch, signum):
    """A signal received inside a log write must unwind before logging or cleanup."""
    manager = process_manager.UvicornProcessManager()
    handlers = {}
    in_log_write = False
    shutdowns = []
    messages = []
    monkeypatch.setattr(
        process_manager.signal,
        "signal",
        lambda value, handler: handlers.__setitem__(value, handler),
    )

    def log(message, *args):
        assert not in_log_write, "Signal handler reentered logging"
        messages.append(message % args if args else message)

    def stop():
        assert not in_log_write, "Cleanup ran inside the interrupted log write"
        shutdowns.append(True)
        handlers[signal.SIGTERM](signal.SIGTERM, None)
        handlers[signal.SIGINT](signal.SIGINT, None)

    class InterruptedProcess:
        def join(self):
            nonlocal in_log_write
            in_log_write = True
            try:
                handlers[signum](signum, None)
            finally:
                in_log_write = False

    manager.processes = [InterruptedProcess()]
    monkeypatch.setattr(process_manager.logger, "info", log)
    monkeypatch.setattr(manager, "stop_all_instances", stop)

    with pytest.raises(SystemExit) as stopped:
        manager.wait_for_termination()

    assert stopped.value.code == 0
    assert shutdowns == [True]
    assert messages == [f"Received signal {signum}, shutting down…"]
