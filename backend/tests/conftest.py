"""Shared pytest fixtures and path setup for the DriveboardApp test suite.

The backend modules import each other with bare names (``import config``,
``import driveboard``, ``from config import conf``), so the ``backend``
directory must be on ``sys.path`` before any of them can be imported. This
file does that at collection time so individual test modules can simply
``import config`` / ``import jobimport`` / etc.
"""

import os
import sys

import pytest

BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
REPO_ROOT = os.path.abspath(os.path.join(BACKEND_DIR, ".."))
TESTJOBS_DIR = os.path.join(BACKEND_DIR, "testjobs")
LIBRARY_DIR = os.path.join(REPO_ROOT, "library")

if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)


@pytest.fixture
def testjobs_dir():
    """Absolute path to backend/testjobs (svg/dxf/dba fixtures)."""
    return TESTJOBS_DIR


@pytest.fixture
def library_dir():
    """Absolute path to the repo-level library/ of .dba jobs."""
    return LIBRARY_DIR


@pytest.fixture
def isolated_config(tmp_path):
    """Point config.conf at throwaway conf/stor directories.

    Snapshots and fully restores ``config.conf`` and ``config.configpath`` on
    teardown so that ``config.load()`` (which mutates ``conf`` in place) cannot
    leak state into other tests or touch the developer's real
    ``~/.driveboardapp`` config.
    """
    import copy

    import config

    conf_backup = copy.deepcopy(config.conf)
    configpath_backup = config.configpath

    confdir = tmp_path / "conf"
    stordir = tmp_path / "stor"
    confdir.mkdir()
    stordir.mkdir()
    config.conf["confdir"] = str(confdir)
    config.conf["stordir"] = str(stordir)
    try:
        yield config
    finally:
        config.conf.clear()
        config.conf.update(conf_backup)
        config.configpath = configpath_backup


@pytest.fixture
def loop(monkeypatch):
    """A real SerialLoopClass wired in as the active driveboard.SerialLoop.

    The thread is never started; tests drive its read/write methods directly.
    """
    import driveboard

    sl = driveboard.SerialLoopClass()
    # A controller accepting a new job has reported its buffers idle.
    sl._status["ready"] = True
    monkeypatch.setattr(driveboard, "SerialLoop", sl)
    return sl


@pytest.fixture
def app():
    """A webtest client wrapping the real bottle WSGI app (routes registered)."""
    import bottle
    import web  # noqa: F401  (import registers routes on the default app)
    import webtest

    return webtest.TestApp(bottle.default_app())
