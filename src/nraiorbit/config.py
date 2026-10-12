"""Configuration loading: pipeline.yaml (+ per-project override) and navarasa.yaml."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_DIR = REPO_ROOT / "config"


def deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_yaml(path: Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_pipeline_config(project_dir: Path | None = None, config_dir: Path = DEFAULT_CONFIG_DIR) -> dict:
    cfg = load_yaml(config_dir / "pipeline.yaml")
    if project_dir is not None and (project_dir / "pipeline.yaml").exists():
        cfg = deep_merge(cfg, load_yaml(project_dir / "pipeline.yaml"))
    return cfg


def load_navarasa(config_dir: Path = DEFAULT_CONFIG_DIR) -> dict:
    return load_yaml(config_dir / "navarasa.yaml")
