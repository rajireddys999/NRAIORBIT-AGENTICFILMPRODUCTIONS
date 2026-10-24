"""Lip-sync is never paid twice for the same job: a job is named by what it is (the paid clip, its take, the crop and
the lines in it), not by the bytes of the crop the pipeline cuts again in every fresh container, which can differ
by a pixel from one machine to the next (a $9.84 re-pay on monkey-saves-man-2). Results paid for under the old byte
keys are still found."""

import json
from types import SimpleNamespace as N

import pytest

from nraiorbit.costing import SpendLedger
from nraiorbit.post import facelock
from nraiorbit.providers.sync_so import SyncSoLipSync


class NoNetwork:
    def post(self, *a, **k):
        raise AssertionError("no job may be submitted: the result is cached")

    def get(self, *a, **k):
        raise AssertionError("no job may be polled: the result is cached")


def _provider(tmp_path):
    cfg = {"lipsync": {"project": "p", "bucket": "b", "poll_seconds": 0}}
    ledger = SpendLedger(tmp_path / "reports" / "cost.jsonl", 10.0)
    return SyncSoLipSync(tmp_path, cfg, {"sync_so": {"per_second": {"sync-3": 0.133}}}, ledger, N(), NoNetwork()), ledger


def _files(tmp_path, name, video=b"crop", audio=b"line"):
    v, a = tmp_path / f"{name}.mp4", tmp_path / f"{name}.wav"
    v.write_bytes(video)
    a.write_bytes(audio)
    return v, a


def test_a_jobs_meaning_keys_it_whatever_its_bytes(tmp_path):
    prov, _ = _provider(tmp_path)
    v1, a1 = _files(tmp_path, "one", b"crop made on one machine")
    v2, a2 = _files(tmp_path, "two", b"crop made on another, a pixel off")
    assert prov.key(v1, a1, "shot 3, take 2, crop 0-96") == prov.key(v2, a2, "shot 3, take 2, crop 0-96")
    assert prov.key(v1, a1, "shot 3, take 2, crop 0-96") != prov.key(v1, a1, "shot 3, take 2, crop 0-120")
    assert prov.key(v1, a1) != prov.key(v2, a2)                          # by bytes, as before


def test_a_result_paid_for_under_its_bytes_is_found_by_its_meaning(tmp_path):
    prov, ledger = _provider(tmp_path)
    v, a = _files(tmp_path, "crop")
    prov.cache.mkdir(parents=True)
    (prov.cache / f"{prov.key(v, a)}.mp4").write_bytes(b"synced, paid before meanings")
    out = prov.apply_many([(v, a, tmp_path / "out" / "synced.mp4", "shot 3 crop 0")])[0]
    assert out.read_bytes() == b"synced, paid before meanings" and ledger.records() == []
    assert (prov.cache / f"{prov.key(v, a, 'shot 3 crop 0')}.mp4").exists()     # kept under its meaning from now on
    v2, a2 = _files(tmp_path, "remade", b"the same crop cut again, a pixel off")
    again = prov.apply_many([(v2, a2, tmp_path / "out" / "again.mp4", "shot 3 crop 0")])[0]
    assert again.read_bytes() == b"synced, paid before meanings" and ledger.records() == []
    assert prov.cached(v2, a2, meaning="shot 3 crop 0") is not None
    assert prov.apply_many([(v, a, tmp_path / "out" / "old.mp4")])[0] is not None   # three-item jobs still work


def test_face_tracking_is_found_under_the_clips_meaning(tmp_path):
    analysis = facelock.Analysis(320, 180, 24.0, 2, [])
    (tmp_path / "faces").mkdir()
    (tmp_path / "faces" / "abc.v2.json").write_text(analysis.to_json(), encoding="utf-8")
    got = facelock.analyse(tmp_path / "re-made.mp4", detector=None, cache_dir=tmp_path / "faces", key="abc.v2")
    assert (got.width, got.height, got.frames) == (320, 180, 2)           # the re-made clip is never read


@pytest.fixture
def pipeline(tmp_path):
    from nraiorbit.config import load_pipeline_config
    from nraiorbit.demo import STORY_BIBLE
    from nraiorbit.pipeline import Pipeline
    from nraiorbit.schemas import StoryBible
    root = tmp_path / "rain"
    (root / "inbox" / "video").mkdir(parents=True)
    (root / "01_story_bible.json").write_text(StoryBible.model_validate(STORY_BIBLE).model_dump_json())
    (root / "inbox" / "video" / "sc001_s001.mp4").write_bytes(b"the clip Veo was paid for")
    return Pipeline(root, None, cfg=load_pipeline_config(), log=lambda *_: None)


def test_a_jobs_meaning_holds_across_containers_and_changes_with_its_lines(pipeline, tmp_path):
    lock = N(characters={"arjun": N(voice_asset="voices/arjun@v1")})
    blocks = {"sc001_b002": N(character_id="arjun", text_te="నేను వస్తాను")}
    voice = tmp_path / "take_1.wav"
    voice.write_bytes(b"the voiced line, a copy of the voice cache")
    line = N(ref_id="sc001_b002", start_s=10.2, duration_s=2.5, gain_db=0.0, path=voice)
    here, there = tmp_path / "here" / "take_1.mp4", tmp_path / "there" / "take_1.mp4"
    for clip, content in ((here, b"re-encoded in one container"), (there, b"re-encoded in another, a pixel off")):
        clip.parent.mkdir()
        clip.write_bytes(content)

    def meaning(clip, lines):
        pipeline._source_sha = {}
        ts = N(shot_id="sc001_s001", clip=clip, in_s=0.0)
        return pipeline._lip_meaning(ts, 4.0, (1920, 1080), 24, lines, 10.0, blocks, lock)
    assert meaning(here, [line]) == meaning(there, [line])                 # the paid clip, not the re-made one
    assert meaning(here, [line]) != meaning(here, [N(**{**vars(line), "start_s": 10.6})])   # the line moved
    before = meaning(here, [line])
    voice.write_bytes(b"voiced again with another voice")
    assert meaning(here, [line])[1] != before[1]                           # a new voice: a new job
    blocks["sc001_b002"] = N(character_id="arjun", text_te="వస్తా")
    assert json.loads(meaning(here, [line])[1])[0][1] == "వస్తా"             # its words are part of it
    (pipeline.p.root / "inbox" / "video" / "sc001_s001_2.mp4").write_bytes(b"take 2, paid for again")
    take2 = tmp_path / "here" / "take_2.mp4"                                 # the take the video step used
    take2.write_bytes(b"re-encoded take 2")
    assert meaning(take2, [line])[0] != meaning(here, [line])[0]


def test_a_meaning_is_a_stable_name(pipeline):
    pipeline._lip_meanings = {3: ("clip", "lines")}
    assert pipeline._meaning(3, "crop", 0, 96, [0, 0, 256, 256]) == pipeline._meaning(3, "crop", 0, 96, [0, 0, 256, 256])
    assert pipeline._meaning(3, "crop", 0, 96, [0, 0, 256, 256]) != pipeline._meaning(3, "whole")
    assert pipeline._meaning(4, "whole") is None                         # a job with no meaning: bytes, as before


def test_a_running_generation_kept_under_its_bytes_is_polled_not_paid_again(tmp_path):
    prov, _ = _provider(tmp_path)
    v, a = _files(tmp_path, "crop")
    prov.cache.mkdir(parents=True)
    (prov.cache / f"{prov.key(v, a)}.pending.json").write_text('{"generation": "gen-7"}')
    k = prov._found(v, a, "shot 3 crop 0")
    assert (prov.cache / f"{k}.pending.json").read_text() == '{"generation": "gen-7"}'
    assert not (prov.cache / f"{prov.key(v, a)}.pending.json").exists()      # moved: one poller, one bill


@pytest.mark.parametrize("stable", [True, False])
def test_providers_that_take_names_get_them_and_others_get_the_old_call(pipeline, tmp_path, monkeypatch, stable):
    import yaml

    from nraiorbit.post import assemble, ffmpeg
    from nraiorbit.schemas import SceneScript
    root = pipeline.p.root
    (root / "03_scenes").mkdir()
    (root / "03_scenes" / "sc001.json").write_text(SceneScript.model_validate({
        "scene_id": "sc001", "heading": "h", "synopsis": "s", "rasa_primary": "veera", "intensity": 5,
        "blocks": [{"block_id": "sc001_b002", "kind": "dialogue", "character_id": "arjun", "text_te": "నేను వస్తాను",
                    "text_en": "I will come", "delivery": None}]}).model_dump_json())
    (root / "cast.lock.yaml").write_text(yaml.safe_dump({"lock_version": 1, "characters": {
        "arjun": {"character_asset": "arjun_x", "voice_asset": "voices/arjun@v1", "costumes": {}}}}))
    pipeline.cfg = {**pipeline.cfg, "lipsync": {**(pipeline.cfg.get("lipsync") or {}), "speaker_lock": False}}
    clip = root / "07_video" / "sc001_s001" / "take_1.mp4"
    clip.parent.mkdir(parents=True)
    clip.write_bytes(b"conformed take 1")
    voice = tmp_path / "line.wav"
    voice.write_bytes(b"voiced")
    monkeypatch.setattr(ffmpeg, "place_audio", lambda events, length, out: out.parent.mkdir(parents=True, exist_ok=True)
                        or out.write_bytes(b"mix") or out)
    monkeypatch.setattr(ffmpeg, "conform_clip", lambda raw, out, *a, **k: raw)
    got = []

    class Syncer:
        stable_keys = stable

        def apply_many(self, items):
            got.extend(items)
            return [it[2] for it in items]
    ts = assemble.TimelineShot("sc001_s001", "sc001", 0.0, 4.0, clip)
    shot = N(shot_id="sc001_s001", block_ids=["sc001_b002"], characters_in_frame=["arjun"])
    line = assemble.AudioEvent(voice, 0.2, 2.5, "dialogue", ref_id="sc001_b002")
    pipeline._lip_sync(Syncer(), [ts], [(0.0, shot)], [(0.0, 4.0)], [line], {"sc001_b002": "arjun"}, (1920, 1080), 24)
    assert len(got) == 1 and len(got[0]) == (4 if stable else 3)
    if stable:
        assert got[0][3] == pipeline._meaning(0, "whole")                  # the clip and its line, by meaning
