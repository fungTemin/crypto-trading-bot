from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from src.config.schema import AppConfig


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base. Lists are replaced, not merged."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _env_override(config: dict, prefix: str = "") -> dict:
    """Override config values from environment variables.
    Maps env vars like TRADING_MODE, EXCHANGE_ID, MARKET_TYPE directly.
    Nested keys use double underscore: STRATEGY__ACTIVE.
    """
    result = config.copy()
    for env_key, env_val in os.environ.items():
        if env_key.startswith("ANTHROPIC_") or env_key.startswith("CLAUDE_"):
            continue
        if "__" in env_key:
            parts = env_key.lower().split("__")
            d = result
            for part in parts[:-1]:
                if part not in d:
                    d[part] = {}
                d = d[part]
            d[parts[-1]] = _coerce_env_value(env_val)
        elif env_key.lower() in result:
            result[env_key.lower()] = _coerce_env_value(env_val)
    return result


def _coerce_env_value(value: str) -> Any:
    """Convert string env var to appropriate Python type."""
    lowered = value.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    if lowered in ("null", "none"):
        return None
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value


def load_config(
    config_dir: str | Path = "config",
    mode: str | None = None,
) -> AppConfig:
    """Load configuration with layered overrides.

    Priority (lowest to highest):
        1. config/default.yaml
        2. config/{mode}.yaml
        3. Environment variables

    The mode is determined by (in order):
        1. Explicit mode argument
        2. TRADING_MODE env var
        3. Default: "paper"
    """
    config_dir = Path(config_dir)

    load_dotenv()

    mode = mode or os.getenv("TRADING_MODE", "paper")

    merged: dict = {}

    # Layer 1: defaults
    default_path = config_dir / "default.yaml"
    if default_path.exists():
        with open(default_path, encoding="utf-8") as f:
            merged = yaml.safe_load(f) or {}

    # Layer 2: mode-specific
    mode_path = config_dir / f"{mode}.yaml"
    if mode_path.exists():
        with open(mode_path, encoding="utf-8") as f:
            mode_config = yaml.safe_load(f) or {}
        merged = _deep_merge(merged, mode_config)

    # Layer 3: env vars
    merged = _env_override(merged)

    return AppConfig(**merged)
