"""Tests for the configuration module (defaults + load/write round-trips)."""

import json
import os


def test_defaults_present():
    import config

    assert config.conf["appname"] == "driveboardapp"
    assert isinstance(config.conf["network_port"], int)
    assert len(config.conf["workspace"]) == 3
    # conf_defaults is a snapshot mirroring the live defaults at import time.
    assert config.conf_defaults["network_port"] == config.conf["network_port"]


def test_userconfigurable_keys_are_real_conf_keys():
    import config

    for key in config.userconfigurable:
        assert key in config.conf, f"{key} documented but missing from conf"


def test_load_creates_default_config_file(isolated_config):
    config = isolated_config
    config.load("")  # empty name -> config.json, auto-created
    expected = os.path.join(config.conf["confdir"], "config.json")
    assert os.path.exists(expected)
    with open(expected) as fp:
        data = json.load(fp)
    # Only user-configurable keys are written out.
    for key in data:
        assert key in config.userconfigurable
    assert "users" not in data  # security-sensitive, never serialized


def test_load_applies_user_overrides(isolated_config):
    config = isolated_config
    path = os.path.join(config.conf["confdir"], "config.testcfg.json")
    with open(path, "w") as fp:
        json.dump({"feedrate": 1234, "grid_mm": 25}, fp)

    config.load("testcfg")
    assert config.conf["feedrate"] == 1234
    assert config.conf["grid_mm"] == 25


def test_load_ignores_non_userconfigurable_keys(isolated_config):
    config = isolated_config
    before_port = config.conf["network_port"]
    path = os.path.join(config.conf["confdir"], "config.evil.json")
    with open(path, "w") as fp:
        # network_port is NOT user-configurable; must be ignored on load.
        json.dump({"network_port": 9999, "feedrate": 4321}, fp)

    config.load("evil")
    assert config.conf["network_port"] == before_port
    assert config.conf["feedrate"] == 4321


def test_write_config_fields_round_trip(isolated_config):
    config = isolated_config
    # load() exits if a named config file is missing, so seed it first.
    path = os.path.join(config.conf["confdir"], "config.rt.json")
    with open(path, "w") as fp:
        json.dump({}, fp)
    config.load("rt")  # establishes configpath
    config.write_config_fields({"feedrate": 777})
    with open(config.configpath) as fp:
        data = json.load(fp)
    assert data["feedrate"] == 777
