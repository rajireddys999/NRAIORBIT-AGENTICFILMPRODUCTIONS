"""Asset library: load manifests, validate, search, and resolve lockfile references.

Manifests live at library/<kind dir>/<id>/manifest.yaml. Binaries referenced from
`files` live next to the manifest (Git LFS / DVC / object storage in production).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ..schemas import ASSET_TYPES, AssetBase

KIND_DIRS = {
    "character": "characters",
    "voice": "voices",
    "music_stem": "music/stems",
    "motif": "music/motifs",
    "action": "actions",
    "location": "locations",
    "sfx": "sfx",
}


@dataclass
class SearchHit:
    asset: AssetBase
    score: float


@dataclass
class ValidationReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


class AssetRegistry:
    def __init__(self, root: Path, assets: dict[str, AssetBase], manifest_dirs: dict[str, Path]):
        self.root = root
        self.assets = assets                  # key: "<kind>:<id>"
        self.manifest_dirs = manifest_dirs

    # ------------------------------------------------------------------ loading

    @classmethod
    def load(cls, root: str | Path) -> "AssetRegistry":
        root = Path(root)
        assets: dict[str, AssetBase] = {}
        dirs: dict[str, Path] = {}
        for kind, sub in KIND_DIRS.items():
            for manifest in sorted((root / sub).glob("*/manifest.yaml")):
                with open(manifest, encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
                data.setdefault("kind", kind)
                if data["kind"] != kind:
                    raise ValueError(f"{manifest}: kind '{data['kind']}' does not match directory '{sub}'")
                asset = ASSET_TYPES[kind].model_validate(data)
                key = f"{kind}:{asset.id}"
                if key in assets:
                    raise ValueError(f"duplicate asset id {key} ({manifest})")
                assets[key] = asset
                dirs[key] = manifest.parent
        return cls(root, assets, dirs)

    def get(self, kind: str, asset_id: str) -> AssetBase:
        try:
            return self.assets[f"{kind}:{asset_id}"]
        except KeyError:
            raise KeyError(f"no {kind} asset '{asset_id}' in library {self.root}") from None

    def resolve(self, ref: str) -> AssetBase:
        """Resolve a lockfile reference like 'characters/arjun_hero@v3'."""
        path, _, version = ref.partition("@v")
        kind_dir, _, asset_id = path.rpartition("/")
        kind = next((k for k, d in KIND_DIRS.items() if d == kind_dir), None)
        if kind is None:
            raise KeyError(f"unknown kind directory in ref '{ref}'")
        asset = self.get(kind, asset_id)
        if version and int(version) != asset.version:
            raise ValueError(
                f"lockfile pins {ref} but library has v{asset.version}; re-run casting or restore v{version}")
        return asset

    def ref(self, asset: AssetBase) -> str:
        return f"{KIND_DIRS[asset.kind]}/{asset.id}@v{asset.version}"

    def file_path(self, asset: AssetBase, file_key: str) -> Path:
        return self.manifest_dirs[f"{asset.kind}:{asset.id}"] / asset.files[file_key]

    # ------------------------------------------------------------------ search

    def search(
        self,
        kind: str,
        *,
        tags: list[str] | None = None,
        rasa: str | None = None,
        intensity: int | None = None,
        bpm: int | None = None,
        production_only: bool = False,
        k: int = 5,
    ) -> list[SearchHit]:
        """Score = 0.5 tag overlap + 0.3 rasa match + 0.2 range fit (intensity / bpm).

        Production deployments blend in embedding similarity (pgvector/Qdrant); the
        signature stays the same.
        """
        tags = [t.lower() for t in (tags or [])]
        hits: list[SearchHit] = []
        for key, asset in self.assets.items():
            if not key.startswith(f"{kind}:"):
                continue
            if production_only and not asset.usable_for_production()[0]:
                continue
            asset_tags = {t.lower() for t in asset.tags}
            tag_score = len(asset_tags & set(tags)) / len(tags) if tags else 0.0
            rasa_score = 1.0 if (rasa and rasa in asset.rasas) else 0.0
            range_scores = []
            if intensity is not None and asset.intensity:
                lo, hi = asset.intensity
                gap = 0 if lo <= intensity <= hi else min(abs(intensity - lo), abs(intensity - hi))
                range_scores.append(max(0.0, 1 - gap / 5))
            asset_bpm = getattr(asset, "bpm", None)
            if bpm is not None and asset_bpm:
                range_scores.append(max(0.0, 1 - abs(bpm - asset_bpm) / 40))
            range_score = sum(range_scores) / len(range_scores) if range_scores else 0.0
            score = 0.5 * tag_score + 0.3 * rasa_score + 0.2 * range_score
            if score > 0:
                hits.append(SearchHit(asset, round(score, 4)))
        hits.sort(key=lambda h: (-h.score, h.asset.id))
        return hits[:k]

    # ------------------------------------------------------------------ validation

    def validate(self, strict_files: bool = False) -> ValidationReport:
        report = ValidationReport()
        for key, asset in sorted(self.assets.items()):
            ok, problems = asset.usable_for_production()
            for p in problems:
                report.warnings.append(f"{key}: {p}")
            for file_key, rel in asset.files.items():
                path = self.manifest_dirs[key] / rel
                if not path.exists():
                    msg = f"{key}: missing file '{file_key}' -> {rel}"
                    (report.errors if strict_files else report.warnings).append(msg)
            default_voice = getattr(asset, "default_voice", None)
            if default_voice and f"voice:{default_voice}" not in self.assets:
                report.errors.append(f"{key}: default_voice '{default_voice}' not in library")
            identity_from = getattr(asset, "identity_from", None)
            if identity_from and f"character:{identity_from.asset}" not in self.assets:
                report.errors.append(f"{key}: identity_from asset '{identity_from.asset}' not in library")
            elif identity_from and identity_from.asset == asset.id:
                report.errors.append(f"{key}: identity_from refers to itself")
        return report
