"""Shared test setup."""
import logging


def pytest_configure(config):
    # Keep test runs out of the real app.log — MagicMock lines in the live
    # log mislead debugging of real incidents and the restart script's tail.
    import logger  # noqa: F401  (import installs the file handler we remove)
    lg = logging.getLogger("voicedictate")
    for h in list(lg.handlers):
        lg.removeHandler(h)
    lg.addHandler(logging.NullHandler())
