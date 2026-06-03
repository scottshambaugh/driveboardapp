"""Frontend smoke tests: every static asset the app serves loads with 200.

Exercises the bottle static-file routes against the real frontend tree so a
missing or mis-routed asset (the kind of breakage a refactor causes) is caught.
"""

import glob
import os

import pytest
import web
from config import conf

# `app` fixture is provided by conftest.py.
# Mirror web.py's choice of frontend vs frontend_mill rather than hardcoding.
FRONTEND_DIR = os.path.join(conf["rootdir"], web.frontend_path)


def _asset_routes():
    """(url, filesystem path) for every served JS/CSS asset."""
    routes = []
    for path in glob.glob(os.path.join(FRONTEND_DIR, "*.js")):
        routes.append(("/" + os.path.basename(path), path))
    for path in glob.glob(os.path.join(FRONTEND_DIR, "js", "*.js")):
        routes.append(("/js/" + os.path.basename(path), path))
    for path in glob.glob(os.path.join(FRONTEND_DIR, "css", "*.css")):
        routes.append(("/css/" + os.path.basename(path), path))
    return routes


def test_index_serves_app_html(app):
    resp = app.get("/")
    assert resp.status_int == 200
    body = resp.body.decode("utf-8", "ignore").lower()
    assert "<html" in body or "<!doctype" in body


def test_static_assets_discovered():
    assert _asset_routes(), "expected frontend js/css assets to serve"


@pytest.mark.parametrize("url", [r[0] for r in _asset_routes()])
def test_static_asset_served(app, url):
    resp = app.get(url)
    assert resp.status_int == 200, url


def test_missing_asset_404s(app):
    resp = app.get("/this-asset-does-not-exist.js", expect_errors=True)
    assert resp.status_int == 404
