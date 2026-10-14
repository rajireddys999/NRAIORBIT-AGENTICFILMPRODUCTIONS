"""Identity packs drawn from another character's portrait (identity_from), and `identity build --only`."""

import json
from types import SimpleNamespace

import pytest
import yaml

from nraiorbit.identity import build_identity_pack, images_to_make, plan_identity_pack
from nraiorbit.library.registry import AssetRegistry
from nraiorbit.schemas import CastLock, StoryBible

CHANGE = "about eight years older, a thick black moustache, grey at the temples, no scar above the eyebrow"
MANIFESTS = {
    "arjun_hero": {"name": "Arjun", "costumes": [{"id": "cst_village", "description": "white shirt, checked veshti"}]},
    "raghavaiah_father": {"name": "Raghavaiah", "identity_from": {"asset": "arjun_hero", "change": CHANGE}},
    "bhairava_villain": {"name": "Bhairava"},
    "bhairava_young": {"name": "Young Bhairava",
                       "identity_from": {"asset": "bhairava_villain", "change": "the same person 24 years younger"}},
}
# Story Bible order puts the derived characters before the ones they are drawn from.
CAST = {"raghavaiah": "raghavaiah_father", "bhairava_young": "bhairava_young", "arjun": "arjun_hero",
        "bhairava": "bhairava_villain"}


def _spec(cid, age):
    return {"id": cid, "name_te": cid, "name_en": cid.title(), "role": "family",
            "physical": {"gender": "male", "age": age, "height_cm": 175, "build": "lean", "skin_tone": "wheatish",
                         "face": "a small scar above the left eyebrow", "hair": "short black", "marks": "none"},
            "costume_baseline": "a cotton shirt", "personality": "", "arc": "", "voice": {
                "pitch": "low", "timbre": "warm", "dialect": "godavari", "speech_register": "rustic",
                "code_mix": "none", "speech_traits": []}}


@pytest.fixture
def film(tmp_path):
    lib_root = tmp_path / "library"
    for asset_id, extra in MANIFESTS.items():
        folder = lib_root / "characters" / asset_id
        folder.mkdir(parents=True)
        (folder / "manifest.yaml").write_text(yaml.safe_dump({"id": asset_id, "kind": "character", **extra}))
    bible = StoryBible.model_validate({
        "title_te": "t", "title_en": "t", "logline": "", "genres": [], "tone": "", "structure_template": "family_drama",
        "era_and_region": "the Godavari delta, 1990s", "world": "", "themes": [], "acts": [],
        "characters": [_spec(cid, 30) for cid in CAST]})
    lock = CastLock(characters={cid: {"character_asset": f"characters/{a}@v1"} for cid, a in CAST.items()})
    return SimpleNamespace(library=AssetRegistry.load(lib_root), lib_root=lib_root, bible=bible, lock=lock)


class FakeImages:
    def __init__(self):
        self.calls = []

    def generate(self, prompt, out, *, refs=(), aspect_ratio="16:9", item="image"):
        self.calls.append({"prompt": prompt, "out": out, "refs": list(refs), "item": item})
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"png")
        return out


def _front(film, asset_id):
    return film.lib_root / "characters" / asset_id / "refs" / "face" / "01_front.png"


def test_derived_portrait_is_drawn_from_the_reference_after_it(film):
    images = FakeImages()
    jobs = plan_identity_pack(film.bible, film.lock, film.library, log=lambda m: None)
    assert [j.asset.id for j in jobs] == ["arjun_hero", "raghavaiah_father", "bhairava_villain", "bhairava_young"]
    planned = images_to_make(jobs)
    build_identity_pack(film.bible, film.lock, film.library, images, log=lambda m: None)
    assert [c["out"] for c in images.calls] == planned               # the preview counts exactly what is made
    fronts = [c for c in images.calls if c["item"].endswith(" front")]
    assert [c["item"] for c in fronts] == ["identity arjun front", "identity raghavaiah front",
                                           "identity bhairava front", "identity bhairava_young front"]
    father = fronts[1]
    assert father["refs"] == [_front(film, "arjun_hero")]
    assert "same person, or a close family resemblance, as in the reference image, but " + CHANGE in father["prompt"]
    assert "entirely fictional 30-year-old Telugu male" in father["prompt"]
    assert "scar" not in father["prompt"].replace(CHANGE, "")          # the description's face is not repeated
    assert fronts[0]["refs"] == [] and "scar above the left eyebrow" in fronts[0]["prompt"]
    assert fronts[3]["refs"] == [_front(film, "bhairava_villain")]
    assert images_to_make(plan_identity_pack(film.bible, film.lock, film.library, log=lambda m: None)) == []
    assert len(images_to_make(jobs, force=True)) == len(planned)


def test_only_builds_just_the_named_characters(film):
    logs = []
    jobs = plan_identity_pack(film.bible, film.lock, film.library, only=["raghavaiah"], log=logs.append)
    assert [j.spec.id for j in jobs] == ["raghavaiah"]
    assert images_to_make(jobs) == []                 # Arjun's portrait is missing and is not built along
    images = FakeImages()
    build_identity_pack(film.bible, film.lock, film.library, images, only=["raghavaiah"], log=logs.append)
    assert images.calls == [] and any("build that character first" in m for m in logs)

    _front(film, "arjun_hero").parent.mkdir(parents=True)
    _front(film, "arjun_hero").write_bytes(b"png")
    jobs = plan_identity_pack(film.bible, film.lock, film.library, only=["raghavaiah"], log=logs.append)
    assert len(images_to_make(jobs)) == 2             # front + three-quarter, nothing for anyone else
    build_identity_pack(film.bible, film.lock, film.library, images, only=["raghavaiah"], log=logs.append)
    assert [c["item"] for c in images.calls] == ["identity raghavaiah front", "identity raghavaiah three-quarter"]
    assert images.calls[0]["refs"] == [_front(film, "arjun_hero")]


def test_only_with_reference_in_the_same_run_counts_both(film):
    jobs = plan_identity_pack(film.bible, film.lock, film.library, only=["bhairava_young", "bhairava"])
    assert [j.spec.id for j in jobs] == ["bhairava", "bhairava_young"]
    assert len(images_to_make(jobs)) == 4


def test_unknown_ids_and_loops_are_refused(film):
    with pytest.raises(ValueError, match="not in the Story Bible: nobody"):
        plan_identity_pack(film.bible, film.lock, film.library, only=["nobody"])
    manifest = film.lib_root / "characters" / "arjun_hero" / "manifest.yaml"
    data = yaml.safe_load(manifest.read_text())
    manifest.write_text(yaml.safe_dump({**data, "identity_from": {"asset": "raghavaiah_father", "change": "younger"}}))
    with pytest.raises(ValueError, match="identity_from loop"):
        plan_identity_pack(film.bible, film.lock, AssetRegistry.load(film.lib_root), log=lambda m: None)


def test_library_validate_checks_identity_from(film):
    assert film.library.validate().ok
    manifest = film.lib_root / "characters" / "bhairava_young" / "manifest.yaml"
    data = yaml.safe_load(manifest.read_text())
    manifest.write_text(yaml.safe_dump({**data, "identity_from": {"asset": "nobody", "change": "x"}}))
    report = AssetRegistry.load(film.lib_root).validate()
    assert any("identity_from asset 'nobody' not in library" in e for e in report.errors)


def test_cli_only_previews_and_builds_just_those(film, tmp_path, monkeypatch, capsys):
    pytest.importorskip("google.genai")
    from nraiorbit import cli
    from nraiorbit.providers import google_ai

    calls = []

    def content(*, model, contents, config):
        calls.append(contents)
        part = SimpleNamespace(inline_data=SimpleNamespace(data=b"\x89PNG fake", mime_type="image/png"))
        return SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=[part]), finish_reason="STOP")],
                               prompt_feedback=None)

    fake = SimpleNamespace(models=SimpleNamespace(generate_content=content))
    root = tmp_path / "projects" / "film"
    root.mkdir(parents=True)
    (root / "01_story_bible.json").write_text(json.dumps(film.bible.model_dump()))
    (root / "cast.lock.yaml").write_text(yaml.safe_dump(film.lock.model_dump()))
    monkeypatch.setattr(cli, "PROJECTS", tmp_path / "projects")
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)                      # the library under test, not the repo's
    monkeypatch.setattr(google_ai, "CLIENT_FACTORY", lambda cfg: fake)

    cli.main(["identity", "build", "film", "--only", "bhairava, bhairava_young", "--yes"])
    out = capsys.readouterr().out
    assert "for bhairava, bhairava_young: 4 image(s)" in out and "$0.16" in out
    assert "(existing ones are kept; --force regenerates)" in out
    assert len(calls) == 4
    assert not _front(film, "arjun_hero").exists() and _front(film, "bhairava_young").exists()
    cli.main(["identity", "build", "film", "--only", "bhairava_young", "--yes"])
    assert "nothing to generate" in capsys.readouterr().out and len(calls) == 4
    cli.main(["identity", "build", "film", "--only", "bhairava_young", "--force", "--yes"])
    out = capsys.readouterr().out
    assert "(existing ones are regenerated)" in out and "kept" not in out and len(calls) > 4
    with pytest.raises(SystemExit, match="not in the Story Bible: ghost"):
        cli.main(["identity", "build", "film", "--only", "ghost", "--yes"])
