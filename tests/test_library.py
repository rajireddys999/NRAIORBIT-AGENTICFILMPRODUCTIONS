import pytest

from nraiorbit.config import REPO_ROOT
from nraiorbit.library.registry import AssetRegistry


@pytest.fixture(scope="module")
def lib():
    return AssetRegistry.load(REPO_ROOT / "library")


def test_library_loads_every_kind(lib):
    kinds = {k.split(":")[0] for k in lib.assets}
    assert kinds == {"character", "voice", "music_stem", "motif", "action", "location", "sfx"}


def test_example_library_is_valid_without_media(lib):
    report = lib.validate(strict_files=False)
    assert report.ok, report.errors
    assert any("missing file" in w for w in report.warnings)   # media is not committed
    assert not lib.validate(strict_files=True).ok


def test_search_ranks_by_tags_rasa_and_ranges(lib):
    hits = lib.search("action", tags=["hero_intro"], rasa="veera", intensity=8)
    assert hits[0].asset.id == "hero_slowmo_entry"
    grooves = lib.search("music_stem", tags=["groove"], rasa="veera", bpm=130)
    assert grooves[0].asset.id == "dappu_teenmaar_132"


def test_resolve_pins_versions(lib):
    asset = lib.resolve("characters/arjun_hero@v3")
    assert asset.name == "Arjun"
    assert lib.ref(asset) == "characters/arjun_hero@v3"
    with pytest.raises(ValueError, match="re-run casting"):
        lib.resolve("characters/arjun_hero@v2")


def test_production_filter_requires_licence_and_consent(lib):
    voice = lib.get("voice", "arjun_voice")
    assert voice.usable_for_production()[0]
    unconsented = voice.model_copy(update={"consent": voice.consent.model_copy(update={"document_ref": None})})
    ok, problems = unconsented.usable_for_production()
    assert not ok and "consent" in problems[0]
