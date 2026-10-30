"""Reels and Shorts: upright whole moments cut from the finished film itself (nothing paid), 5 to 15 seconds from
one cut to a later cut of a scene with no line cut off, over real shots from before the ending, as many as the owner
asks for, made again only when the film or that number changes; and YouTube Shorts uploaded once each, apart from the
film's own upload, the rest after YouTube's daily limit resets."""

import json
import subprocess
from types import SimpleNamespace as N

import pytest

from nraiorbit.demo import SHOTS, STORY_BIBLE
from nraiorbit.post import ffmpeg, reels
from nraiorbit.post.trailer import SPOILER_CUTOFF, TrailerLine
from nraiorbit.schemas import ShotList, StoryBible
from nraiorbit.service import queue, shorts

BIBLE = StoryBible.model_validate(STORY_BIBLE)
BASE = ShotList.model_validate({"scene_id": "sc001", "shots": SHOTS["sc001"]}).shots[0]


def _film(seconds=60.0, shot_s=5.0, placeholder=(), lines=(), punch=()):
    """An edl, shot plans and placed lines: shots of shot_s back to back, lines as (id, start, duration)."""
    shots, plans = [], {}
    t, n = 0.0, 0
    while t < seconds - 1e-6:
        n += 1
        sid = f"sc001_s{n:03d}"
        shots.append({"shot_id": sid, "scene_id": f"sc{1 + int(t // 20):03d}", "start_s": t, "duration_s": shot_s,
                      "real": sid not in placeholder})
        carried = [lid for lid, at, dur in lines if t <= at < t + shot_s]
        plans[sid] = BASE.model_copy(update={"shot_id": sid, "block_ids": carried, "intensity": 6,
                                             "punch_dialogue": any(lid in punch for lid in carried)})
        t += shot_s
    edl = {"film_seconds": seconds, "shots": shots,
           "dialogue": [{"ref_id": lid, "start_s": at, "duration_s": dur} for lid, at, dur in lines]}
    placed = [TrailerLine(lid, at, dur, BIBLE.characters[0].id, "veera", 7) for lid, at, dur in lines]
    return edl, plans, placed


ROLES = {c.id: c.role for c in BIBLE.characters}


def test_each_reel_is_a_whole_moment_cut_to_cut_with_every_line_whole():
    lines = [("b01", 3.0, 2.0), ("b02", 12.4, 2.5), ("b03", 16.0, 3.0), ("b04", 31.0, 2.0), ("b05", 40.4, 2.2),
             ("b06", 55.0, 2.0), ("b07", 24.0, 2.0)]              # b06 is after the spoiler cutoff; b03 crosses 20 s
    edl, plans, placed = _film(lines=lines, placeholder={"sc001_s007"}, punch={"b05"})   # 30-35 s is a placeholder
    cuts = {round(s["start_s"], 3) for s in edl["shots"]} | {round(s["start_s"] + s["duration_s"], 3)
                                                             for s in edl["shots"]}
    scene_at = {round(s["start_s"], 3): s["scene_id"] for s in edl["shots"]}
    for count in (1, 3, 6):
        picked = reels.plan_reels(edl, plans, placed, ROLES, count=count)
        assert 1 <= len(picked) <= count and [p["start_s"] for p in picked] == sorted(p["start_s"] for p in picked)
        for p in picked:
            a, b = p["start_s"], round(p["start_s"] + p["duration_s"], 3)
            assert a in cuts and b in cuts                                    # starts and ends where the film cuts
            assert reels.SHORTEST_S - 1e-6 <= p["duration_s"] <= reels.LONGEST_S + 1e-6
            assert b <= SPOILER_CUTOFF * 60 + 1e-6 and not (a < 35.0 and b > 30.0)   # no ending, no placeholder
            assert {scene_at[round(t, 3)] for t in (a, b - 5.0)} == {p["scene_id"]}  # inside one scene
            for lid, at, dur in lines:                                        # no line is cut off at either end
                assert at + dur <= a + 1e-6 or at >= b - 1e-6 or (a <= at and at + dur <= b)
        ends = [(p["start_s"], p["start_s"] + p["duration_s"]) for p in picked]
        assert all(nxt[0] >= end + reels.GAP_S - 1e-6 for (_, end), nxt in zip(ends, ends[1:]))   # never overlapping
        assert "b05" in {p["line_id"] for p in picked}                        # the punch line comes first
        assert not {"b06", "b04"} & {p["line_id"] for p in picked}
    three = reels.plan_reels(edl, plans, placed, ROLES, count=3)
    assert len({p["scene_id"] for p in three}) == len(three)                  # spread over the film first


def test_with_too_few_lines_strong_picture_makes_up_the_number_and_a_short_film_gets_none():
    edl, plans, placed = _film(lines=[("b01", 10.0, 2.0)])
    picked = reels.plan_reels(edl, plans, placed, ROLES)
    assert len(picked) == reels.REEL_COUNT and sum(p["line_id"] is None for p in picked) == reels.REEL_COUNT - 1
    assert reels.plan_reels(*_film(seconds=4.0), ROLES) == []


def test_the_caption_names_the_film_and_says_it_is_ai_made():
    text = reels.caption(BIBLE, "నేను వస్తాను", "I will come")
    assert BIBLE.title_te in text and BIBLE.title_en in text and "“నేను వస్తాను”" in text and "I will come" in text
    assert reels.AI_TAG in text and len(reels.HASHTAGS) <= 5 and all(h in text for h in reels.HASHTAGS)


def test_the_overlay_is_an_upright_transparent_frame(tmp_path):
    from PIL import Image
    out = reels.render_overlay(tmp_path / "o.png", BIBLE.title_te, BIBLE.title_en, "నేను వస్తాను", "I will come")
    im = Image.open(out)
    assert im.size == reels.REEL_SIZE and im.mode == "RGBA"
    assert im.getpixel((540, 960))[3] == 0 and im.getpixel((540, 5))[3] > 100        # clear middle, dark top band
    assert im.getpixel((540, 1880))[3] > 100                                 # the bottom band reaches the edge


def _project(tmp_path, monkeypatch):
    root = tmp_path / "rain"
    (root / "11_master").mkdir(parents=True)
    (root / "reports").mkdir()
    (root / "04_shots").mkdir()
    (root / "03_scenes").mkdir()
    edl, plans, _ = _film(lines=[])
    (root / "reports" / "edl.json").write_text(json.dumps(edl))
    (root / "04_shots" / "sc001.json").write_text(ShotList(scene_id="sc001", shots=list(plans.values()))
                                                 .model_dump_json())
    (root / "01_story_bible.json").write_text(BIBLE.model_dump_json())
    cut = []

    def cut_reel(src, start, length, overlay, out, fps):
        cut.append(start)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"reel")
        return out
    monkeypatch.setattr(reels, "cut_reel", cut_reel)
    return root, cut


def test_reels_are_made_once_per_film_and_again_when_the_film_changes(tmp_path, monkeypatch):
    root, cut = _project(tmp_path, monkeypatch)
    assert reels.make_reels(root, log=lambda *_: None) == "skipped: no finished film yet"
    (root / "11_master" / "movie.mp4").write_bytes(b"the film")
    assert reels.make_reels(root, log=lambda *_: None) == "3 made" and len(cut) == 3
    record = json.loads((root / "11_master" / "reels" / "reels.json").read_text())
    assert [r["file"] for r in record["reels"]] == ["reel_1.mp4", "reel_2.mp4", "reel_3.mp4"]
    assert all(BIBLE.title_en in r["caption"] for r in record["reels"])
    assert reels.make_reels(root, log=lambda *_: None) == "3 up to date" and len(cut) == 3   # a re-run pays nothing
    (root / "11_master" / "movie.mp4").write_bytes(b"the film, re-cut")
    assert reels.make_reels(root, log=lambda *_: None) == "3 made" and len(cut) == 6
    said = reels.make_reels(root, count=20, log=lambda *_: None)            # the owner asked for more: cut again
    record = json.loads((root / "11_master" / "reels" / "reels.json").read_text())
    kept = {(r["start_s"], r["duration_s"]) for r in record["reels"]} & set(zip(cut[3:6], [5.0] * 3))
    assert len(cut) == 6 + len(record["reels"]) - len(kept)                 # the ones still chosen are not cut again
    assert all(r["fp"]["sha256"] for r in record["reels"])
    assert record["plan"]["count"] == 20 and 3 < len(record["reels"]) < 20
    assert said == f"{len(record['reels'])} made (the film has room for {len(record['reels'])} whole moments, not 20)"
    assert reels.make_reels(root, count=20, log=lambda *_: None).endswith("up to date")


@pytest.mark.skipif(not ffmpeg.available(), reason="needs ffmpeg")
def test_a_reel_is_upright_with_the_films_sound_ready_for_instagram(tmp_path):
    src = tmp_path / "film.mp4"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=24:d=9",
                    "-f", "lavfi", "-i", "sine=frequency=440:d=9", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", str(src)], check=True)
    overlay = reels.render_overlay(tmp_path / "o.png", BIBLE.title_te, BIBLE.title_en, "", "I will come")
    out = reels.cut_reel(src, 2.0, 6.5, overlay, tmp_path / "reel.mp4", 24)
    probe = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json",
                                       str(out)], capture_output=True, text=True, check=True).stdout)
    video = next(s for s in probe["streams"] if s["codec_type"] == "video")
    audio = next(s for s in probe["streams"] if s["codec_type"] == "audio")
    assert (video["width"], video["height"], video["pix_fmt"]) == (1080, 1920, "yuv420p")
    assert video["codec_name"] == "h264" and audio["codec_name"] == "aac" and audio["sample_rate"] == "48000"
    assert abs(float(probe["format"]["duration"]) - 6.5) < 0.1
    data = out.read_bytes()
    assert data.index(b"moov") < data.index(b"mdat")                        # faststart: plays while it loads


# ---------------------------------------------------------------------------------------------- Shorts

class Blob:
    def __init__(self, bucket, name):
        self.bucket, self.name = bucket, name
        self.generation = bucket.gens.get(name)          # as read: a later write does not change it

    def download_as_text(self):
        return self.bucket.objects[self.name].decode()

    def upload_from_string(self, text, content_type=None):
        self.bucket.put(self.name, text.encode() if isinstance(text, str) else text)

    def delete(self, if_generation_match=None):
        from google.api_core.exceptions import NotFound, PreconditionFailed
        if self.name not in self.bucket.objects:
            raise NotFound("gone")
        if if_generation_match is not None and if_generation_match != self.bucket.gens[self.name]:
            raise PreconditionFailed("changed")
        del self.bucket.objects[self.name]


class Bucket:
    def __init__(self):
        self.objects, self.gens, self.n = {}, {}, 0

    def put(self, name, data):
        self.n += 1
        self.objects[name], self.gens[name] = data, self.n

    def blob(self, name):
        return Blob(self, name)

    def get_blob(self, name):
        return Blob(self, name) if name in self.objects else None

    def list_blobs(self, prefix=""):
        return [Blob(self, n) for n in sorted(self.objects) if n.startswith(prefix)]


@pytest.fixture
def cloud(monkeypatch):
    from nraiorbit.release import gcs
    b = Bucket()
    b.put("rain/project/01_story_bible.json", BIBLE.model_dump_json().encode())
    b.put("rain/reports/youtube.json", json.dumps({"film": {"id": "FILM1"}}).encode())
    made = [{"file": f"reel_{n}.mp4", "line_te": "నేను వస్తాను", "line_en": f"line {n}"} for n in (1, 2, 3)]
    b.put("rain/11_master/reels/reels.json", json.dumps({"film": "x", "reels": made}).encode())
    for n in (1, 2, 3):
        b.put(f"rain/11_master/reels/reel_{n}.mp4", f"reel {n}".encode())
    monkeypatch.setattr(gcs, "download_blob", lambda blob, local: local.write_bytes(blob.bucket.objects[blob.name]))
    return b


def _ask(cloud, privacy="public"):
    cloud.put("stories/rain.shorts.json", json.dumps({"by": "red@x", "at": "now", "privacy": privacy}).encode())


def test_the_owners_shorts_go_up_once_each_with_their_metadata_and_the_request_is_taken_once(cloud):
    _ask(cloud)
    sent, kept = [], []

    def upload(video, meta, creds):
        sent.append(meta)
        kept.append(sorted(json.loads(cloud.objects.get("rain/reports/youtube_shorts.json", b"{}"))))
        return f"ID{len(sent)}"

    def rows():
        return [{"slug": "rain", "state": "awaiting:publish"}]
    assert shorts.run(cloud, rows, log=lambda *_: None, upload=upload, creds="c") == ["rain"]
    assert "stories/rain.shorts.json" not in cloud.objects and len(sent) == 3
    assert [sum(k.startswith("reel_") for k in seen) for seen in kept] == [0, 1, 2]   # each recorded before the next
    m = sent[0]
    assert m["snippet"]["title"].endswith("#Shorts") and len(m["snippet"]["title"]) <= 100
    assert m["status"] == {"privacyStatus": "public", "selfDeclaredMadeForKids": False, "containsSyntheticMedia": True}
    assert "https://youtu.be/FILM1" in m["snippet"]["description"] and reels.AI_TAG in m["snippet"]["description"]
    record = json.loads(cloud.objects["rain/reports/youtube_shorts.json"])
    ups = sorted((v for k, v in record.items() if k.startswith("reel_")), key=lambda v: v["reel"])
    assert [v["id"] for v in ups] == ["ID1", "ID2", "ID3"] and record["privacy"] == "public"
    assert all(v["film"] == "x" and v["file"]["sha256"] for v in ups) and "uploading" not in record
    made = json.loads(cloud.objects["rain/11_master/reels/reels.json"])
    assert shorts.current(record, made).keys() == {1, 2, 3} and shorts.current(record, {**made, "film": "y"}) == {}
    assert all(m["snippet"]["title"].endswith(" #Shorts") for m in sent)
    _ask(cloud)                                                            # asked again: nothing new goes up
    assert shorts.run(cloud, rows, log=lambda *_: None, upload=upload, creds="c") == ["rain"] and len(sent) == 3


def test_a_request_is_taken_by_one_tick_only_and_waits_while_a_run_works_on_the_film(cloud):
    _ask(cloud)
    assert shorts.run(cloud, lambda: [{"slug": "rain", "state": "producing"}], upload=None) == []
    assert "stories/rain.shorts.json" in cloud.objects                     # left for the tick after the run
    stale = cloud.get_blob("stories/rain.shorts.json")                     # a second tick read it too
    assert shorts.take(cloud, "rain")["privacy"] == "public"
    assert shorts.take(cloud, "rain") is None                              # a second tick finds nothing to take
    _ask(cloud)                                                            # asked again meanwhile
    from google.api_core.exceptions import PreconditionFailed
    with pytest.raises(PreconditionFailed):
        stale.delete(if_generation_match=stale.generation)                 # the stale read cannot take the new one
    assert shorts.run(Bucket(), rows=lambda: pytest.fail("no request: the sheet is not read")) == []


def test_a_failed_upload_is_written_down_for_the_page_and_the_request_is_not_retried_by_itself(cloud):
    _ask(cloud, privacy="sideways")

    def broken(video, meta, creds):
        assert meta["status"]["privacyStatus"] == "private"                # anything else asked: private
        raise RuntimeError("backendError")
    shorts.run(cloud, lambda: [], log=lambda *_: None, upload=broken, creds="c")
    record = json.loads(cloud.objects["rain/reports/youtube_shorts.json"])
    assert "backendError" in record["error"] and "stories/rain.shorts.json" not in cloud.objects


def test_youtubes_daily_limit_puts_the_rest_off_until_it_resets(cloud):
    """Many Shorts outrun YouTube's daily upload quota: what went up is kept, and the rest are asked for again for
    after the reset, which no tick takes before then."""
    import datetime
    _ask(cloud)
    sent = []

    def upload(video, meta, creds):
        if sent:
            raise RuntimeError('<HttpError 403 "The request cannot be completed because you have exceeded your '
                               'quota." reason "quotaExceeded">')
        sent.append(video.name)
        return "vid1"
    shorts.run(cloud, lambda: [], log=lambda *_: None, upload=upload, creds="c")
    record = json.loads(cloud.objects["rain/reports/youtube_shorts.json"])
    assert [v["id"] for k, v in record.items() if k.startswith("reel_")] == ["vid1"]
    assert "daily upload limit" in record["error"] and record["resume_after"]
    again = json.loads(cloud.objects["stories/rain.shorts.json"])
    assert again["not_before"] == record["resume_after"] and not shorts.due(again)
    assert shorts.run(cloud, lambda: [], log=lambda *_: None, upload=upload, creds="c") == []   # not before then
    later = datetime.datetime.fromisoformat(again["not_before"]) + datetime.timedelta(minutes=1)
    assert shorts.due(again, later)
    at = shorts.after_daily_reset(datetime.datetime(2026, 10, 31, 7, 30, tzinfo=datetime.timezone.utc))
    assert at == "2026-11-01T08:10:00+00:00"                                 # at least an hour away
    assert shorts.after_daily_reset(datetime.datetime(2026, 10, 31, 2, 0, tzinfo=datetime.timezone.utc)) == \
        "2026-10-31T08:10:00+00:00"


def test_a_short_already_up_under_another_number_is_never_uploaded_twice(cloud):
    _ask(cloud)
    from nraiorbit.service.produce import fingerprint
    made = json.loads(cloud.objects["rain/11_master/reels/reels.json"])
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as d:
        first = Path(d) / "r.mp4"
        first.write_bytes(cloud.objects[f"rain/11_master/reels/{made['reels'][0]['file']}"])
        fp = fingerprint(first)
    cloud.put("rain/reports/youtube_shorts.json", json.dumps({"reel_9": {"id": "old", "file": fp}}).encode())
    sent = []
    shorts.run(cloud, lambda: [], log=lambda *_: None, upload=lambda v, m, c: sent.append(v.name) or f"v{len(sent)}",
               creds="c")
    record = json.loads(cloud.objects["rain/reports/youtube_shorts.json"])
    assert record["reel_9"]["id"] == "old" and len(sent) == len(made["reels"]) - 1    # the old one kept, not resent
    assert made["reels"][0]["file"] not in sent


def test_a_tick_hands_the_shorts_step_the_bucket_and_reads_the_sheet_only_when_asked(tmp_path, monkeypatch):
    st, rf = tmp_path / "stories.csv", tmp_path / "references.csv"
    queue.save(str(st), [{"slug": "rain", "state": "awaiting:publish"}], queue.STORY_COLUMNS)
    queue.save(str(rf), [], queue.REFERENCE_COLUMNS)
    seen = []
    monkeypatch.setattr(queue, "SHORTS", lambda bucket, rows, log: seen.append((bucket, rows())) or ["rain"])
    out = queue.tick(str(st), str(rf), tmp_path / "projects", bucket="bkt", produce_fn=None, learn_fn=None,
                     log=lambda *_: None)
    assert out["shorts"] == ["rain"] and seen[0][0] == "bkt" and seen[0][1][0]["slug"] == "rain"
    monkeypatch.setattr(queue, "SHORTS", lambda bucket, rows, log: [])
    assert "shorts" not in queue.tick(str(st), str(rf), tmp_path / "projects", bucket="bkt", produce_fn=None,
                                      learn_fn=None, log=lambda *_: None)


def test_a_request_waits_while_another_tick_is_uploading_the_same_shorts(cloud):
    _ask(cloud)
    cloud.put("rain/reports/youtube_shorts.json", json.dumps({"uploading": {"at": shorts._now()}}).encode())
    assert shorts.run(cloud, lambda: [], upload=None) == [] and "stories/rain.shorts.json" in cloud.objects
    cloud.put("rain/reports/youtube_shorts.json", json.dumps({"uploading": {"at": "2020-01-01T00:00:00+00:00"}})
              .encode())                                                   # a tick killed long ago: asked again
    sent = []
    assert shorts.run(cloud, lambda: [], log=lambda *_: None, upload=lambda v, m, c: sent.append(m) or "ID",
                      creds="c") == ["rain"] and len(sent) == 3


def test_a_long_line_never_pushes_the_shorts_tag_out_of_the_title():
    meta = shorts.metadata(BIBLE, {"line_en": "a very long line " * 12}, "public")
    assert meta["snippet"]["title"].endswith(" #Shorts") and len(meta["snippet"]["title"]) <= 100


def test_a_failed_recut_offers_no_reels_rather_than_a_mix_of_two_cuts(tmp_path, monkeypatch):
    root, cut = _project(tmp_path, monkeypatch)
    (root / "11_master" / "movie.mp4").write_bytes(b"the film")
    assert reels.make_reels(root, log=lambda *_: None) == "3 made"
    (root / "11_master" / "movie.mp4").write_bytes(b"the film, re-cut")

    def broken(src, start, length, overlay, out, fps):
        if len(cut) >= 4:
            raise RuntimeError("ffmpeg failed")
        cut.append(start)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"new reel")
        return out
    monkeypatch.setattr(reels, "cut_reel", broken)
    with pytest.raises(RuntimeError):
        reels.make_reels(root, log=lambda *_: None)
    record = json.loads((root / "11_master" / "reels" / "reels.json").read_text())
    assert record == {"film": None, "reels": []} and not (root / "11_master" / ".reels.new").exists()
    assert {p.read_bytes() for p in (root / "11_master" / "reels").glob("reel_*.mp4")} == {b"reel"}   # none new


def test_the_films_first_line_can_have_a_reel_whatever_frame_it_starts_on():
    for at in (10.0, 10.041667, 10.0454, 10.0456):                          # 24 fps frame times and between
        edl, plans, placed = _film(lines=[(f"b{i}", t, 2.0) for i, t in enumerate((at, 30.0), 1)])
        assert "b1" in {p["line_id"] for p in reels.plan_reels(edl, plans, placed, ROLES)}, at


def test_one_films_unreadable_record_never_holds_up_the_next_films_shorts(cloud):
    for slug in ("aaa", "rain"):
        cloud.put(f"stories/{slug}.shorts.json", b'{"privacy": "public"}')
    cloud.put("aaa/reports/youtube_shorts.json", b"{not json")
    sent = []
    assert shorts.run(cloud, lambda: [], log=lambda *_: None, upload=lambda v, m, c: sent.append(m) or "ID",
                      creds="c") == ["rain"] and len(sent) == 3


def test_shorts_are_automatic_unless_the_owner_or_the_config_says_ask(tmp_path, monkeypatch, cloud):
    from nraiorbit.release import gcs
    root = tmp_path / "rain"
    (root / "11_master" / "reels").mkdir(parents=True)
    (root / "reports").mkdir()
    assert shorts.mode(root, {}) == "auto" and shorts.mode(root, {"reels": {"shorts": "ask"}}) == "ask"
    (root / shorts.MODE_FILE).write_text(json.dumps({"mode": "auto"}))
    assert shorts.mode(root, {"reels": {"shorts": "ask"}}) == "auto"          # the owner's choice for the film wins
    monkeypatch.setattr(gcs, "client", lambda: N(bucket=lambda name: cloud))
    assert shorts.after_film_upload(root, "bkt", {}) is None                  # no reels yet: nothing to ask for
    movie = root / "11_master" / "movie.mp4"
    movie.write_bytes(b"the film")
    (root / "11_master" / "reels" / "reels.json").write_text(json.dumps({"film": "old cut", "reels": [{"file": "r"}]}))
    assert shorts.after_film_upload(root, "bkt", {}).endswith("earlier cut")   # never an older cut's reels
    mark = reels.film_mark(movie)
    (root / "11_master" / "reels" / "reels.json").write_text(json.dumps({"film": mark, "reels": [{"file": "r.mp4"}]}))
    (root / "reports" / "youtube.json").write_text(json.dumps({"film": {"id": "F", "privacy": "unlisted"}}))
    assert shorts.after_film_upload(root, "bkt", {}).startswith("asked for automatically (unlisted)")
    ask = json.loads(cloud.objects["stories/rain.shorts.json"])
    assert ask["privacy"] == "unlisted" and ask["by"].startswith("automatic") and ask["film"] == mark
    (root / "reports" / "youtube.json").write_text(json.dumps({"film": {"id": "F"}}))   # from before: private
    assert shorts.after_film_upload(root, "bkt", {}).startswith("asked for automatically (private)")
    del cloud.objects["stories/rain.shorts.json"]
    (root / "reports" / shorts.RECORD).write_text(json.dumps({"reel_1": {"id": "S", "film": mark}}))
    assert shorts.after_film_upload(root, "bkt", {}) == "all up already"
    (root / shorts.MODE_FILE).write_text(json.dumps({"mode": "ask"}))
    (root / "reports" / shorts.RECORD).unlink()
    assert shorts.after_film_upload(root, "bkt", {}) is None and "stories/rain.shorts.json" not in cloud.objects


def test_a_request_for_one_cut_never_uploads_another_cuts_reels(cloud):
    cloud.put("stories/rain.shorts.json", json.dumps({"privacy": "public", "film": "a newer cut"}).encode())
    sent = []
    shorts.run(cloud, lambda: [], log=lambda *_: None, upload=lambda v, m, c: sent.append(m) or "ID", creds="c")
    record = json.loads(cloud.objects["rain/reports/youtube_shorts.json"])
    assert sent == [] and "another cut" in record["error"]


def test_the_tick_carries_the_owners_shorts_choice_and_sends_what_the_run_asked_for_at_once(tmp_path, monkeypatch):
    st, rf = tmp_path / "stories.csv", tmp_path / "references.csv"
    (tmp_path / "rain.md").write_text("A ferryman and a flood.", encoding="utf-8")
    queue.save(str(st), [{"slug": "rain", "story_uri": str(tmp_path / "rain.md"), "budget_usd": "40", "gates": "ask",
                          "publish": "public", "state": "approved", "note": "", "updated": ""}], queue.STORY_COLUMNS)
    queue.save(str(rf), [], queue.REFERENCE_COLUMNS)
    template = tmp_path / "projects" / "_template"
    template.mkdir(parents=True)
    (template / "pipeline.yaml").write_text("providers: {video: mock}\n", encoding="utf-8")
    calls, seen = [], {}

    def shorts_step(bucket, rows, log):                                    # the second look comes after the row
        calls.append(rows()[0]["state"] if calls else "before")            # is written: the film is done by then
        return ["rain"] if len(calls) > 1 else []
    monkeypatch.setattr(queue, "SHORTS", shorts_step)
    from nraiorbit.ops import backup
    monkeypatch.setattr(backup, "sync_library", lambda *a, **k: None)
    monkeypatch.setattr(backup, "restore_report", lambda root, b, log=None, **k: (0, []))
    monkeypatch.setattr(backup, "backup", lambda *a, **k: 1)

    def fetch(uri):
        if uri.endswith("rain.shorts-mode.json"):
            return json.dumps({"mode": "ask"})
        return queue._fetch(uri) if not uri.startswith("gs://") else ""

    def produce_fn(root, bucket):
        seen["mode"] = json.loads((root / shorts.MODE_FILE).read_text())["mode"]
        return {"state": "done", "steps": {}}
    out = queue.tick(str(st), str(rf), tmp_path / "projects", bucket="bkt", produce_fn=produce_fn, learn_fn=None,
                     template=template, fetch_story=fetch, log=lambda *_: None)
    assert seen["mode"] == "ask" and calls == ["before", "done"] and out["shorts"] == ["rain"]


def test_a_raised_count_uploads_only_the_new_shorts(cloud):
    """Reels renumbered in film order after the owner asked for more: the ones already up never go up again."""
    _ask(cloud)
    sent = []
    shorts.run(cloud, lambda: [], log=lambda *_: None, upload=lambda v, m, c: sent.append(v.read_bytes()) or
               f"v{len(sent)}", creds="c")
    assert len(sent) == 3
    made = [{"file": f"reel_{n}.mp4", "line_en": f"line {n}"} for n in (1, 2, 3, 4, 5)]
    for n, body in enumerate((b"reel 1", b"new a", b"reel 2", b"new b", b"reel 3"), 1):
        cloud.put(f"rain/11_master/reels/reel_{n}.mp4", body)
    cloud.put("rain/11_master/reels/reels.json", json.dumps({"film": "x", "reels": made}).encode())
    _ask(cloud)
    shorts.run(cloud, lambda: [], log=lambda *_: None, upload=lambda v, m, c: sent.append(v.read_bytes()) or
               f"v{len(sent)}", creds="c")
    assert sent[3:] == [b"new a", b"new b"]
    record = json.loads(cloud.objects["rain/reports/youtube_shorts.json"])
    assert len([k for k in record if k.startswith("reel_")]) == 5                # the old Shorts' ids are all kept


def test_reels_keep_clear_of_the_cards_and_of_their_own_fades():
    lines = [("b01", 6.0, 2.0), ("b02", 21.0, 3.7)]                      # b02 ends 0.3 s before 25: inside its fade
    edl, plans, placed = _film(lines=lines)
    picked = reels.plan_reels(edl, plans, placed, ROLES, count=6, cards=[(0.3, 4.3)])
    assert picked and all(p["start_s"] >= 4.3 + reels.CARD_CLEAR_S for p in picked)
    for p in picked:
        b = p["start_s"] + p["duration_s"]
        for lid, at, dur in lines:
            if p["start_s"] <= at and at + dur <= b:
                assert at >= p["start_s"] + reels.FADE_IN_S - 1e-6 and at + dur <= b - reels.FADE_OUT_S + 1e-6


def test_the_cards_are_read_as_the_cut_lays_them(tmp_path):
    import yaml
    edl = {"dialogue": [{"start_s": 10.0, "duration_s": 2.0}]}
    (tmp_path / "pipeline.yaml").write_text(yaml.safe_dump({"cards": "auto"}))
    assert reels.card_spans(tmp_path, edl) == [(0.3, 4.3)]
    (tmp_path / "pipeline.yaml").write_text(yaml.safe_dump({"cards": [
        {"kind": "interval", "at_s": 261.0, "length_s": 2.0, "text_te": "విరామం", "text_en": "INTERVAL"}]}))
    assert reels.card_spans(tmp_path, edl) == [(261.0, 263.0)]
    (tmp_path / "pipeline.yaml").write_text(yaml.safe_dump({"cards": []}))
    assert reels.card_spans(tmp_path, edl) == []
    (tmp_path / "pipeline.yaml").write_text(yaml.safe_dump({"film": {"runtime_minutes": 3}}))   # as queued films
    assert reels.card_spans(tmp_path, edl) == [(0.3, 4.3)]                  # the defaults' cards: auto
    assert reels.card_spans(tmp_path, edl, "auto") == [(0.3, 4.3)] and reels.card_spans(tmp_path, edl, []) == []
