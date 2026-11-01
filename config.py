"""Loads config.yaml once at startup. Environment variables override deployment settings."""

import os

import yaml

with open(os.getenv("CONFIG_PATH", "config.yaml")) as f:
    _cfg = yaml.safe_load(f)

SIMULATOR_URL = os.getenv("SIMULATOR_URL", _cfg["simulator_url"])
DB_PATH = os.getenv("DB_PATH", _cfg["db_path"])

TICK_SECONDS = _cfg["scheduler"]["tick_seconds"]

NOTIFY_TIMEOUT_SEC = _cfg["timeouts"]["notify_timeout_sec"]
MAX_NUDGES = _cfg["timeouts"]["max_nudges"]
RESULT_TIMEOUT_SEC = _cfg["timeouts"]["result_timeout_sec"]

MIN_SAMPLE_SIZE = _cfg["auto_halt"]["min_sample_size"]
