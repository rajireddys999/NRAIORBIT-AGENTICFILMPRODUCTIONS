"""The custom model feed's music: the studio's own tracks in custom_model_feed/bgm/, named by what they are for
(action_bgm_<when>), reused free by any film whose cue they fit, grown by every film's new cues (never a character's
theme), and seeded from a reference's score notes at a price the owner approved first."""

import datetime
import json
from types import SimpleNamespace as N
from pathlib import Path

import pytest

from nraiorbit.schemas import Cue, CueSheet
from nraiorbit.service import feed


def cue(cid="c1", rasa="raudra", intensity=8, bpm=140, instruments=("dhol", "brass"), cue_type="fight",
        duration_s=40.0, **kw):
    return Cue(cue_id=cid, scene_ids=["sc001"], start_s=0.0, duration_s=duration_s, rasa=rasa, intensity=intensity,
               cue_type=cue_type, raga="", tala="adi", bpm=bpm, instruments=list(instruments),
               leitmotif_character=kw.pop("leitmotif_character", None), peak=kw.pop("peak", False), **kw)


@pytest.fixture
def lib(tmp_path, monkeypatch):
    monkeypatch.setattr(feed, "FEED_DIR", tmp_path / "custom_model_feed")
    monkeypatch.setattr(feed, "audio_seconds", lambda path: 0.0 if b"TOO-SHORT" in Path(path).read_bytes() else 45.0)
    return tmp_path / "custom_model_feed"


def _audio(tmp_path, name, data):
    p = tmp_path / name
    p.write_bytes(data)
    return p


def test_a_sound_is_named_for_what_it_is_for_and_kept_once(lib, tmp_path):
    when = datetime.datetime(2026, 10, 25, 21, 5, 12, tzinfo=datetime.timezone.utc)
    assert feed.name_for("action", "bgm", when) == "action_bgm_20261025-210512"
    stored = feed.add(_audio(tmp_path, "raw.m4a", b"drums"), feed.BGM, "action", {"bpm": 140}, when=when)
    assert stored == lib / "bgm" / "action_bgm_20261025-210512.m4a"
    tags = json.loads((lib / "bgm" / "action_bgm_20261025-210512.json").read_text())
    assert tags["mood"] == "action" and tags["bpm"] == 140 and tags["kind"] == "bgm"
    assert feed.add(_audio(tmp_path, "again.m4a", b"drums"), feed.BGM, "action", {}, when=when) == stored   # once
    other = feed.add(_audio(tmp_path, "b.m4a", b"strings"), feed.BGM, "sad", {}, when=when)
    assert other.name == "sad_bgm_20261025-210512.m4a"
    third = feed.add(_audio(tmp_path, "c.m4a", b"more drums"), feed.BGM, "action", {}, when=when)
    assert third.name.startswith("action_bgm_20261025-210512-")           # the same second: kept apart
    assert [e["mood"] for e in feed.entries(feed.BGM)].count("action") == 2
    with pytest.raises(ValueError, match="not a usable bgm track"):
        feed.add(_audio(tmp_path, "s.m4a", b"TOO-SHORT"), feed.BGM, "sad", {})   # too short or will not decode: refused


def test_a_cue_gets_the_stored_track_that_fits_it_never_twice_and_never_a_characters_theme(lib, tmp_path):
    assert feed.mood_of(cue(rasa="veera", intensity=9)) == "mass" and feed.mood_of(cue(rasa="karuna", cue_type="lament")) == "sad"
    assert feed.mood_of(cue(rasa="veera", intensity=5, cue_type="chase")) == "action"
    fight = feed.add(_audio(tmp_path, "f.m4a", b"f"), feed.BGM, "action",
                     {"intensity": 8, "bpm": 140, "instruments": ["dhol", "brass"]})
    feed.add(_audio(tmp_path, "s.m4a", b"s"), feed.BGM, "sad", {"intensity": 4, "bpm": 70, "instruments": ["violin"]})
    assert feed.choose(cue(), used=set())["name"] == fight.name
    assert feed.choose(cue(), used={fight.name}) is None                    # one track, one cue per film
    assert feed.choose(cue(rasa="hasya", cue_type="comic"), used=set()) is None   # no comedy track: a new one
    assert feed.choose(cue(intensity=2, bpm=60, instruments=("flute",)), used=set()) is None   # mood alone: not enough
    assert feed.choose(cue(leitmotif_character="chintu"), used=set()) is None
    assert feed.choose(cue(cue_type="trailer"), used=set()) is None          # made for its film alone
    assert feed.choose(cue(notes="hit at 12.4 s, silence at 20"), used=set()) is None
    assert feed.choose(cue(duration_s=300.0), used=set()) is None            # 45 s would loop under 5 minutes


def _lyria(tmp_path, monkeypatch, root, library="prefer"):
    from nraiorbit.costing import SpendLedger
    from nraiorbit.providers import lyria
    monkeypatch.setattr(lyria, "fit_to_cue", lambda src, seconds, out, start=0.0: out.write_bytes(src.read_bytes()) and out)
    monkeypatch.setattr(lyria, "alignment", lambda raw, c, seconds: 0.0)
    paid = []

    def generate(self, c, prompt):
        paid.append(c.cue_id)
        return _audio(tmp_path, f"gen-{root.name}-{c.cue_id}.m4a", f"{root.name}:{c.cue_id}".encode())
    monkeypatch.setattr(lyria.LyriaMusic, "_generate", generate)
    root.mkdir(parents=True, exist_ok=True)
    music = lyria.LyriaMusic(root, {"google": {}, "music": {"library": library}}, {}, SpendLedger(root / "c.jsonl", None))
    return music, paid


def test_a_film_reuses_a_fitting_feed_track_free_and_adds_its_new_cues_for_the_next(lib, tmp_path, monkeypatch):
    first, paid = _lyria(tmp_path, monkeypatch, tmp_path / "film1")
    first.render_cue(cue("c1"), tmp_path / "out1.wav")
    first.render_cue(cue("c2", leitmotif_character="chintu"), tmp_path / "out2.wav")
    first.render_cue(cue("c3", bpm=142), tmp_path / "out2b.wav")              # never its own c1 back
    first.render_cue(cue("tr", cue_type="trailer"), tmp_path / "out2c.wav")
    assert paid == ["c1", "c2", "c3", "tr"]
    names = [e["name"] for e in feed.entries(feed.BGM)]
    assert len(names) == 2 and all(n.startswith("action_bgm_") for n in names)   # the theme and the trailer stay
    assert json.loads((lib / "bgm" / (names[0].rsplit(".", 1)[0] + ".json")).read_text())["made_for"] == \
        {"film": "film1", "cue": "c1"}
    second, paid2 = _lyria(tmp_path, monkeypatch, tmp_path / "film2")
    out = second.render_cue(cue("x1"), tmp_path / "out3.wav")
    assert paid2 == [] and out.read_bytes().startswith(b"film1:c")          # free, from the feed
    chosen = json.loads((tmp_path / "film2" / "reports" / "music_feed.json").read_text())
    assert len(chosen) == 1 and list(chosen.values())[0] in names
    for track in (lib / "bgm").iterdir():                                   # the owner clears the feed: the film
        track.unlink()                                                     # keeps its own copy, under the cue
    again = second.render_cue(cue("x1"), tmp_path / "out4.wav")
    assert paid2 == [] and again.read_bytes() == out.read_bytes()          # the same music every render
    second.render_cue(cue("x1", rasa="karuna", cue_type="lament"), tmp_path / "out5.wav")
    assert paid2 == ["x1"]                                                 # a changed cue is asked again
    off, paid3 = _lyria(tmp_path, monkeypatch, tmp_path / "film3", library="off")
    off.render_cue(cue("y1"), tmp_path / "out6.wav")
    assert paid3 == ["y1"] and not any((e.get("made_for") or {}).get("film") == "film3"
                                       for e in feed.entries(feed.BGM))     # off: nothing it makes is kept


def test_a_reference_seeds_the_feed_with_tracks_written_from_its_score_notes(lib, tmp_path):
    (lib / "notes" / "kalki").mkdir(parents=True)
    (lib / "notes" / "kalki" / "profile.json").write_text(json.dumps({"title": "Kalki", "score": {"motifs": "x"},
                                                                       "cautions": ["the theme melody"]}))
    asked = []

    class Director:
        def library_cues(self, profile, count):
            asked.append((profile["title"], count, profile["cautions"]))
            return CueSheet(cues=[cue("a", leitmotif_character="hero"), cue("b", rasa="karuna", cue_type="sad"),
                                  cue("c", rasa="hasya", cue_type="comic")])

    class Music:
        model = "lyria-3-pro-preview"

        def __init__(self):
            self.n = 0

        def prompt(self, c):
            return f"prompt for {c.cue_id}"

        def _generate(self, c, prompt):
            self.n += 1
            if self.n == 3:
                from nraiorbit.costing import BudgetExceeded
                raise BudgetExceeded("the approved amount is spent")
            return _audio(tmp_path, f"{c.cue_id}.m4a", c.cue_id.encode())
    made = feed.build_music("kalki", 3, Director(), Music(), log=lambda *_: None)
    assert asked == [("Kalki", 3, ["the theme melody"])]                    # its cautions go to the director
    assert [m.split("_bgm_")[0] for m in made] == ["action", "sad"]         # stopped at the approved amount
    tags = [e for e in feed.entries(feed.BGM)]
    assert all(t["made_for"] == {"reference": "kalki"} for t in tags)
    assert feed.build_price(10, 0.08) == 1.00


def test_an_approved_build_is_taken_once_and_its_result_left_for_the_page(lib, monkeypatch):
    from nraiorbit.ops import backup
    synced = []
    monkeypatch.setattr(backup, "sync_library", lambda bucket, direction, sets=(), log=None, **k: synced.append(
        (direction, tuple(sets))))
    from test_reels import Bucket
    cloud = Bucket()
    cloud.put("queue/feed/kalki.bgm.json", json.dumps({"by": "red@x", "count": 10, "price": 0.95,
                                                       "approved_usd": 1.09}).encode())
    calls = []
    out = feed.run_builds("bkt", lambda pid, count, usd, log: calls.append((pid, count, usd)) or ["action_bgm_1.m4a"],
                          log=lambda *_: None, storage=cloud)
    assert out == ["kalki"] and calls == [("kalki", 10, 1.09)] and "queue/feed/kalki.bgm.json" not in cloud.objects
    result = json.loads(cloud.objects["custom_model_feed/builds/kalki.bgm.json"])
    assert result["made"] == ["action_bgm_1.m4a"] and result["asked"]["price"] == 0.95
    assert synced == [("restore", ("references", "bgm", "sfx")), ("backup", ("bgm",))]
    assert feed.run_builds("bkt", lambda *a: pytest.fail("nothing asked"), storage=cloud) == []



def test_a_build_whose_tracks_could_not_go_up_says_so_instead_of_vanishing(lib, monkeypatch):
    from nraiorbit.ops import backup
    from test_reels import Bucket

    def down(bucket, direction, sets=(), log=None, **k):
        if direction == "backup":
            raise OSError("503 backend error")
    monkeypatch.setattr(backup, "sync_library", down)
    cloud = Bucket()
    cloud.put("queue/feed/kalki.bgm.json", json.dumps({"count": 2, "price": 0.36, "approved_usd": 0.41}).encode())
    feed.run_builds("bkt", lambda pid, count, usd, log: {"made": ["a.m4a"], "spent_usd": 0.28}, log=lambda *_: None,
                    storage=cloud)
    result = json.loads(cloud.objects["custom_model_feed/builds/kalki.bgm.json"])
    assert result["made"] == ["a.m4a"] and result["spent_usd"] == 0.28 and "did not reach the bucket" in result["error"]



def test_the_sound_effects_starter_pack_is_made_by_name_within_the_approved_amount(lib, tmp_path):
    from nraiorbit.costing import SpendLedger
    names = [n for n, _, _ in feed.STARTER_SFX]
    assert len(names) == len(set(names)) >= 30 and feed.sfx_price(len(names), 0.03) == round(len(names) * 0.03, 2)
    sent = []

    class Http:
        def post(self, url, headers, json, timeout):
            sent.append((url, headers["xi-api-key"], json))
            return N(status_code=200, content=b"mp3 " + json["text"].encode())
    ledger = SpendLedger(tmp_path / "sfx.jsonl", 0.07)                       # two sounds' worth, at $0.03
    made = feed.build_sfx(feed.STARTER_SFX[:4], ledger, 0.03, "k3y", http=Http(), log=lambda *_: None,
                          work=tmp_path / "w")
    assert [m.split("_sfx_")[0] for m in made] == ["punch", "kick"] and len(sent) == 2   # stopped at the amount
    assert sent[0][0].endswith("/v1/sound-generation") and sent[0][1] == "k3y"
    assert "duration_seconds" not in sent[0][2] and sent[0][2]["text"].endswith("about 1.5 seconds")   # flat price
    tags = feed.entries(feed.SFX)
    assert {t["mood"] for t in tags} == {"punch", "kick"} and all(t["made_for"] == {"pack": "starter"} for t in tags)
    assert round(ledger.spent(), 2) == 0.06


def test_an_approved_sound_effects_build_goes_to_its_own_maker(lib, monkeypatch):
    from nraiorbit.ops import backup
    from test_reels import Bucket
    monkeypatch.setattr(backup, "sync_library", lambda *a, **k: None)
    cloud = Bucket()
    cloud.put("queue/feed/starter.sfx.json", json.dumps({"count": 40, "price": 1.2, "approved_usd": 1.32}).encode())
    calls = []
    feed.run_builds("bkt", lambda *a: pytest.fail("not a music build"), log=lambda *_: None, storage=cloud,
                    sfx_fn=lambda pack, count, usd, log: calls.append((pack, count, usd)) or {"made": ["punch_sfx_1.mp3"],
                                                                                             "spent_usd": 0.03})
    assert calls == [("starter", 40, 1.32)]
    assert json.loads(cloud.objects["custom_model_feed/builds/starter.sfx.json"])["made"] == ["punch_sfx_1.mp3"]



def test_a_second_build_makes_only_what_is_missing_and_a_refused_or_lost_call_is_handled(lib, tmp_path):
    import requests

    from nraiorbit.costing import SpendLedger
    replies = iter([N(status_code=200, content=b"slap"), requests.Timeout("read timed out"),
                    N(status_code=401, content=b"", text="invalid api key")])
    sent = []

    class Http:
        def post(self, url, headers, json, timeout):
            sent.append(json["text"])
            r = next(replies)
            if isinstance(r, Exception):
                raise r
            return r
    feed.add(_audio(tmp_path, "p.mp3", b"punch audio"), feed.SFX, "punch", {})          # made by an earlier build
    feed.add(_audio(tmp_path, "k.mp3", b"kick audio"), feed.SFX, "kick", {})
    ledger = SpendLedger(tmp_path / "sfx.jsonl", 5.0)
    made = feed.build_sfx(feed.STARTER_SFX[:6], ledger, 0.03, "k", http=Http(), log=lambda *_: None, work=tmp_path / "w")
    assert [m.split("_sfx_")[0] for m in made] == ["slap"]                  # punch and kick: never made again
    assert len(sent) == 3 and sent[1].startswith("a person falling")         # body_fall timed out: counted
    records = ledger.records()
    assert [r["item"] for r in records] == ["slap", "body_fall"] and "unconfirmed" in records[1]["note"]
    assert "sword_draw" not in str(made) and sent[2].startswith("two steel swords")   # the 401 stopped it there
    assert feed.missing_sfx({"punch", "kick", "slap"})[0][0] == "body_fall"
