"""Configuration loading: pipeline.yaml (+ the film's tool set, + per-project override) and navarasa.yaml."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_DIR = REPO_ROOT / "config"
QUALITY_FILE = "quality.yaml"


class UnknownQuality(ValueError):
    """A film names a tool set config/quality.yaml does not have."""


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


@lru_cache(maxsize=4)
def _quality_sets(config_dir: str) -> dict:
    path = Path(config_dir) / QUALITY_FILE
    return load_yaml(path) if path.exists() else {}


def quality_sets(config_dir: Path = DEFAULT_CONFIG_DIR) -> dict:
    """The named tool sets of config/quality.yaml: {key: {label, about, about_usd_per_minute, settings}}."""
    return _quality_sets(str(config_dir))


def quality_names(config_dir: Path = DEFAULT_CONFIG_DIR) -> list[str]:
    return list(quality_sets(config_dir))


def quality_key(value) -> str | None:
    """A tool set's key as written in a sheet or a form (HD, " sd "), None for nothing."""
    name = str(value or "").strip().lower()
    return name or None


def with_quality(base: dict, own: dict, config_dir: Path = DEFAULT_CONFIG_DIR) -> dict:
    """`base`, then the settings of the tool set `own["quality"]` names, then `own`: the set fills in the tools a
    film does not name itself, and anything the film's own file says wins over the set. No quality: base then own,
    as before. A quality that is not a set stops here, by name, rather than running on the wrong tools."""
    key = quality_key(own.get("quality"))
    if key is None:
        return deep_merge(base, own)
    sets = quality_sets(config_dir)
    if key not in sets:
        raise UnknownQuality(f"quality '{own.get('quality')}' is not a tool set; pick one of "
                             f"{', '.join(sets) or 'none (config/quality.yaml is missing)'}")
    cfg = deep_merge(deep_merge(base, (sets[key] or {}).get("settings") or {}), own)
    cfg["quality"] = key
    return cfg


def load_pipeline_config(project_dir: Path | None = None, config_dir: Path = DEFAULT_CONFIG_DIR) -> dict:
    cfg = load_yaml(config_dir / "pipeline.yaml")
    own = load_yaml(project_dir / "pipeline.yaml") if project_dir is not None \
        and (project_dir / "pipeline.yaml").exists() else {}
    return with_quality(cfg, own, config_dir)


def load_navarasa(config_dir: Path = DEFAULT_CONFIG_DIR) -> dict:
    return load_yaml(config_dir / "navarasa.yaml")
