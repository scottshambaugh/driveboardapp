"""Run the frontend javascript suite under node, as part of the normal pytest run.

The ui decides which controls are live, what the run button says and whether a
stop condition is showing, and none of that is reachable from the Python
tests. Those checks live in ``frontend/tests`` and run under node's own test
runner, so this hands them to pytest rather than leaving them to be
remembered.

Self-skips when node is unavailable, the way the firmware tests do for
avr-gcc. CI has a frontend job with node installed, so the suite still runs on
every push either way.
"""

import glob
import os
import shutil
import subprocess

import pytest
from config import conf

NODE = shutil.which("node")
JS_TEST_DIR = os.path.join(conf["rootdir"], "frontend", "tests")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")


def _js_test_files():
    return sorted(glob.glob(os.path.join(JS_TEST_DIR, "*.test.js")))


def test_js_suite_is_discovered():
    assert _js_test_files(), f"expected javascript tests in {JS_TEST_DIR}"


@pytest.mark.parametrize("path", _js_test_files(), ids=os.path.basename)
def test_js_suite(path):
    result = subprocess.run(
        [NODE, "--test", path],
        capture_output=True,
        text=True,
        cwd=conf["rootdir"],
    )
    if result.returncode != 0:
        pytest.fail(result.stdout + result.stderr, pytrace=False)
