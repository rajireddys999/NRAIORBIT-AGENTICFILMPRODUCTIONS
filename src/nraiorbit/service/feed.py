"""The custom model feed: everything the studio's agents learn from and reuse, kept in the bucket under
custom_model_feed/ and brought down to every run with the library (ops/backup.sync_library).

    custom_model_feed/notes/<id>/profile.json     craft notes Gemini wrote from a reference video (service/learn)
    custom_model_feed/bgm/<mood>_bgm_<when>.<ext>  background music the studio generated (Lyria), with <name>.json
    custom_model_feed/sfx/<mood>_sfx_<when>.<ext>  sound effects the studio generated, with <name>.json

Every sound here is our own: generated from our prompts (a cue sheet, or a reference's craft notes with its
"do not copy" list), never a reference's audio. Its name says what it is for (action_bgm_20261025-210512.m4a), and
its tags file says the rest (emotion, intensity, tempo, raga, instruments, what it was made for or from), so a film
can find the track a scene needs. A film reuses a matching track instead of paying for a new one
(providers/lyria.py), and a new track that is not a character's own theme joins the feed for the next film.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import shutil
from pathlib import Path

from ..config import REPO_ROOT

FEED_DIR = REPO_ROOT / "custom_model_feed"
BGM, SFX, NOTES = "bgm", "sfx", "notes"
AUDIO = (".m4a", ".mp3", ".wav", ".ogg", ".flac", ".aac")

# the emotion a cue carries (its rasa), in the words the files are named by
RASA_MOOD = {"shringara": "romance", "hasya": "comedy", "karuna": "sad", "raudra": "action", "veera": "heroic",
             "bhayanaka": "suspense", "bibhatsa": "dark", "adbhuta": "wonder", "shanta": "calm",
             "vatsalya": "family", "bhakti": "devotional"}
MOODS = ("action", "mass", "heroic", "romance", "comedy", "sad", "suspense", "dark", "wonder", "calm", "family",
         "devotional")


def mood_of(cue) -> str:
    """What a cue is for, in one word: a heroic peak is mass (the elevation), a fight or chase is action, else its
    emotion's word."""
    kind = str(getattr(cue, "cue_type", "") or "").lower()
    if cue.rasa == "veera" and (getattr(cue, "peak", False) or cue.intensity >= 8):
        return "mass"
    if any(w in kind for w in ("fight", "chase", "action", "battle")):
        return "action"
    return RASA_MOOD.get(cue.rasa, "calm")


def folder(kind: str, root: Path | None = None) -> Path:
    return Path(root or FEED_DIR) / kind


def name_for(mood: str, kind: str, when: datetime.datetime | None = None) -> str:
    """<mood>_<kind>_<YYYYMMDD-HHMMSS>: what it is for, then when it was made."""
    when = when or datetime.datetime.now(datetime.timezone.utc)
    return f"{mood}_{kind}_{when:%Y%m%d-%H%M%S}"


MIN_SECONDS = {"bgm": 10.0, "sfx": 0.2}    # audio shorter than this (or that will not decode) is never kept


def audio_seconds(path: Path) -> float | None:
    """The length of an audio file (ffmpeg), None when it will not decode."""
    from ..post import ffmpeg
    try:
        return round(float(ffmpeg.duration(Path(path))), 2)
    except Exception:
        return None


def reusable(cue) -> bool:
    """Whether a cue may take a shared track, or give one: not a character's own theme, not the trailer, and not a
    cue carrying its own music direction (a timed arc, the owner's notes), which are made for their film alone."""
    return not getattr(cue, "leitmotif_character", None) and str(getattr(cue, "cue_type", "")) != "trailer" \
        and not str(getattr(cue, "notes", "") or "").strip()


def add(src: Path, kind: str, mood: str, tags: dict, root: Path | None = None,
        when: datetime.datetime | None = None) -> Path:
    """Put a sound into the feed under its name, with its tags beside it; the same audio is never added twice
    (its content hash is in the tags), and audio that will not decode or is too short is refused (ValueError).
    Returns the stored file."""
    src = Path(src)
    seconds = audio_seconds(src)
    if seconds is None or seconds < MIN_SECONDS.get(kind, 0.0):
        raise ValueError(f"{src.name} is not a usable {kind} track ({seconds} s)")
    digest = hashlib.sha256(src.read_bytes()).hexdigest()[:16]
    dest_dir = folder(kind, root)
    for meta in dest_dir.glob("*.json"):
        try:
            if json.loads(meta.read_text(encoding="utf-8")).get("sha") == digest:
                return next((p for p in dest_dir.glob(meta.stem + ".*") if p.suffix in AUDIO), meta)
        except (OSError, ValueError):
            continue
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = name_for(mood, kind, when)
    if any(dest_dir.glob(name + ".*")):                   # two in the same second: the hash keeps them apart
        name = f"{name}-{digest[:4]}"
    dest = dest_dir / f"{name}{src.suffix.lower() or '.wav'}"
    shutil.copyfile(src, dest)
    record = {"name": dest.name, "kind": kind, "mood": mood, "sha": digest, "audio_seconds": seconds,
              "created": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"), **tags}
    (dest_dir / f"{name}.json").write_text(json.dumps(record, indent=1, ensure_ascii=False), encoding="utf-8")
    return dest


def entries(kind: str, root: Path | None = None) -> list[dict]:
    """Every sound of a kind in the feed, with its tags and file (oldest first, by name's time)."""
    out = []
    for meta in sorted(folder(kind, root).glob("*.json")):
        try:
            rec = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        audio = next((p for p in meta.parent.glob(meta.stem + ".*") if p.suffix in AUDIO), None)
        if audio is not None and isinstance(rec, dict):
            out.append({**rec, "path": audio, "stem": meta.stem})
    return sorted(out, key=lambda r: (r.get("created") or "", r["stem"]))


def score(cue, rec: dict) -> float:
    """How well a stored track fits a cue (0 = not at all): the same mood first, then intensity, tempo, raga and
    instruments."""
    if rec.get("mood") != mood_of(cue):
        return 0.0
    s = 1.0
    s += max(0.0, 1 - abs(int(rec.get("intensity") or 5) - cue.intensity) / 5)
    bpm = float(rec.get("bpm") or 0)
    s += max(0.0, 1 - abs(bpm - cue.bpm) / max(cue.bpm, 1) * 2) if bpm and cue.bpm else 0.0
    s += 0.5 if rec.get("raga") and rec.get("raga") == cue.raga else 0.0
    mine, theirs = {i.lower() for i in cue.instruments}, {str(i).lower() for i in rec.get("instruments") or []}
    s += len(mine & theirs) / max(len(mine), 1)
    return round(s, 3)


GOOD_ENOUGH = 2.5       # the mood and more (close intensity, tempo or instruments); below it a new cue is made
MIN_SHARE = 0.6         # a track shorter than this share of the cue would loop too often under it


def choose(cue, used: set[str], film: str | None = None, root: Path | None = None) -> dict | None:
    """The best stored track for a cue: never one this film already uses or made itself, never one much shorter
    than the cue; among equals, a different one for each film (not always the oldest). None when nothing fits well
    enough (GOOD_ENOUGH) or the cue may not share (reusable)."""
    if not reusable(cue):
        return None
    need = max(0.5, float(cue.duration_s) - float(getattr(cue, "silence_before_s", 0.0) or 0.0))

    def fits(r: dict) -> bool:
        own = film is not None and (r.get("made_for") or {}).get("film") == film
        return r["name"] not in used and not own and float(r.get("audio_seconds") or need) >= MIN_SHARE * need

    def spread(r: dict) -> str:                          # a fixed order per film and cue, a different one per film
        return hashlib.sha1(f"{film}:{cue.cue_id}:{r['name']}".encode()).hexdigest()
    ranked = sorted(((score(cue, r), r) for r in entries(BGM, root) if fits(r)), key=lambda t: (-t[0], spread(t[1])))
    return ranked[0][1] if ranked and ranked[0][0] >= GOOD_ENOUGH else None


def cue_tags(cue, prompt: str, model: str, made_for: dict) -> dict:
    """What a track generated for a cue is, for its tags file."""
    return {"rasa": cue.rasa, "intensity": cue.intensity, "bpm": cue.bpm, "raga": cue.raga, "tala": cue.tala,
            "instruments": list(cue.instruments), "cue_type": cue.cue_type, "seconds": round(float(cue.duration_s), 1),
            "prompt": prompt, "model": model, "made_for": made_for}


# ---------------------------------------------------------------------------------------------- builds from references

REQUESTS = "queue/feed/"         # queue/feed/<reference id>.bgm.json: the owner asked (and approved the price)
PLAN_USD = 0.20                  # the music director's planning call, at most (its output is capped)
PLAN_MAX_TOKENS = 8000
MAX_TRACKS = 20
DEFAULT_BUILD = 10               # tracks the References page offers to build from one reference


def build_price(count: int, lyria_usd: float) -> float:
    """What building `count` tracks from a reference costs: one planning call, then one generation each."""
    return round(PLAN_USD + count * lyria_usd, 2)


def build_music(pid: str, count: int, director, music, log=print, root: Path | None = None) -> list[str]:
    """`count` original tracks into the feed, written by the music director (`director.library_cues`) from the
    reference's notes (notes/<pid>/profile.json) and made by `music` (a LyriaMusic, whose ledger holds the
    approved amount). Returns the names stored; a generation that fails is skipped and logged."""
    if count < 1:
        raise ValueError("a build makes at least one track")
    profile = json.loads((folder(NOTES, root) / pid / "profile.json").read_text(encoding="utf-8"))
    sheet = director.library_cues(profile, count)
    made = []
    for cue in sheet.cues[:count]:
        cue = cue.model_copy(update={"leitmotif_character": None, "scene_ids": [], "start_s": 0.0, "notes": ""})
        prompt = music.prompt(cue)
        try:
            raw = music._generate(cue, prompt)
            stored = add(raw, BGM, mood_of(cue), cue_tags(cue, prompt, music.model, {"reference": pid}), root)
        except Exception as exc:
            log(f"  {cue.cue_id}: not kept ({type(exc).__name__}: {str(exc)[:120]})")
            if type(exc).__name__ == "BudgetExceeded":
                break                                   # the approved amount is spent: stop
            continue
        made.append(stored.name)
        log(f"  {stored.name}: {mood_of(cue)}, {cue.bpm} bpm, {', '.join(cue.instruments[:4])}")
    return made


def take(bucket, name: str) -> dict | None:
    """A build request, removed from the bucket by this tick alone (a delete under the generation it read)."""
    from google.api_core.exceptions import NotFound, PreconditionFailed
    blob = bucket.get_blob(name)
    if blob is None:
        return None
    try:
        ask = json.loads(blob.download_as_text())
    except ValueError:
        ask = {}
    try:
        blob.delete(if_generation_match=blob.generation)
    except (NotFound, PreconditionFailed):
        return None
    return ask if isinstance(ask, dict) else {}


def _no_sfx(*a, **k):
    raise RuntimeError("this runner cannot make sound effects")


def run_builds(bucket_name: str, build_fn, log=print, storage=None, sfx_fn=None) -> list[str]:
    """Every build the owner approved (queue/feed/<reference id>.bgm.json), taken once, made by
    `build_fn(pid, count, approved_usd, log)`, its result written beside the feed (custom_model_feed/builds/<id>.json)
    for the control room, and the feed sent up. Returns the references built from."""
    from ..ops import backup
    bucket = storage or __import__("nraiorbit.release.gcs", fromlist=["client"]).client().bucket(bucket_name)
    asked = sorted(b.name for b in bucket.list_blobs(prefix=REQUESTS) if b.name.endswith((".bgm.json", ".sfx.json")))
    if not asked:
        return []
    backup.sync_library(bucket_name, "restore", sets=("references", "bgm", "sfx"), log=lambda *_: None)
    done = []
    for name in asked:
        ask = take(bucket, name)
        if ask is None:
            continue
        kind = "sfx" if name.endswith(".sfx.json") else "bgm"
        pid = name[len(REQUESTS):-len(f".{kind}.json")]
        result = {"reference": pid, "kind": kind, "asked": {k: ask.get(k) for k in ("by", "at", "count", "price")},
                  "started": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")}
        try:                                            # under way: the page shows it and asks for nothing new
            bucket.blob(f"custom_model_feed/builds/{pid}.{kind}.json").upload_from_string(
                json.dumps(result, indent=1, ensure_ascii=False), content_type="application/json")
        except Exception:
            pass
        try:
            out = (build_fn(pid, int(ask.get("count") or 0), float(ask.get("approved_usd") or 0), log) if kind == "bgm"
                   else (sfx_fn or _no_sfx)(pid, int(ask.get("count") or 0), float(ask.get("approved_usd") or 0), log))
            made, spent = (out.get("made", []), out.get("spent_usd")) if isinstance(out, dict) else (out, None)
            result.update(made=made, spent_usd=spent,
                          finished=datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"))
            if not made:
                result["error"] = f"no {'sound' if kind == 'sfx' else 'track'} was made (see the runner's log)"
            done.append(pid)
        except Exception as exc:
            result["error"] = f"{type(exc).__name__}: {str(exc)[:240]}"
            log(f"  feed build from {pid} stopped: {result['error']}")
        try:                                            # what was paid goes up now, not after every build
            backup.sync_library(bucket_name, "backup", sets=(kind,), log=lambda *_: None)
        except Exception as exc:
            result["error"] = (result.get("error", "") + f" The tracks did not reach the bucket yet ({type(exc).__name__}: "
                               f"{str(exc)[:120]}); the next run sends them.").strip()
        try:
            bucket.blob(f"custom_model_feed/builds/{pid}.{kind}.json").upload_from_string(
                json.dumps(result, indent=1, ensure_ascii=False), content_type="application/json")
        except Exception as exc:
            log(f"  feed build from {pid}: its result was not saved ({type(exc).__name__}: {str(exc)[:120]})")
    return done


# ---------------------------------------------------------------------------------------------- sound effects

# the starter pack: sounds Telugu films use all the time, each with its name (what it is), its prompt and its length
STARTER_SFX = [
    ("punch", "a heavy cinematic punch impact for a Telugu action film, deep body thud with a short whoosh", 1.5),
    ("kick", "a powerful cinematic kick impact, dull body hit with cloth movement", 1.5),
    ("slap", "a sharp loud face slap, cinematic", 1.0),
    ("body_fall", "a person falling hard onto a dirt ground, thud and dust", 2.0),
    ("sword_clash", "two steel swords clashing hard, metallic ring", 2.0),
    ("sword_draw", "a sword drawn from a sheath, metallic scrape", 1.5),
    ("whoosh_fast", "a fast cinematic whoosh for a quick camera move", 1.0),
    ("whoosh_heavy", "a heavy slow cinematic whoosh with low rumble", 2.0),
    ("slowmo_boom", "a deep cinematic slow-motion boom, sub bass hit with long tail, for a hero's elevation moment", 4.0),
    ("hit_stinger", "a short cinematic impact stinger, big hit with metallic shimmer", 2.0),
    ("riser_tension", "a rising cinematic tension riser that builds for four seconds and stops", 4.0),
    ("heartbeat", "a slow tense heartbeat, low and close", 4.0),
    ("glass_break", "a glass window shattering", 2.0),
    ("wood_break", "a wooden plank or door breaking apart", 2.0),
    ("gunshot_distant", "a single distant gunshot echoing across hills", 2.5),
    ("explosion", "a large cinematic explosion with debris", 4.0),
    ("fire_crackling", "a fire crackling and burning steadily", 6.0),
    ("rain_heavy", "heavy monsoon rain falling on leaves and a tin roof", 8.0),
    ("thunder", "a loud rolling thunderclap", 5.0),
    ("wind", "strong wind blowing across open land", 8.0),
    ("river_flow", "a fast river flowing over rocks", 8.0),
    ("flood_water", "rushing flood water, powerful and chaotic", 8.0),
    ("forest_birds", "a quiet South Indian forest morning with birds calling", 10.0),
    ("night_crickets", "a village night with crickets and distant frogs", 10.0),
    ("village_morning", "an Indian village morning, roosters, distant cattle bells, soft voices far away", 10.0),
    ("temple_bells", "temple bells ringing in an Indian temple, with a little reverb", 6.0),
    ("crowd_cheer", "a large crowd cheering and whistling for a hero, Indian festival energy", 6.0),
    ("crowd_murmur", "a crowd murmuring in a village square, indistinct voices", 8.0),
    ("market_bustle", "a busy Indian street market, vendors calling indistinctly, footsteps", 8.0),
    ("jeep_pass", "an old jeep driving past on a dirt road", 5.0),
    ("bike_rev", "a motorcycle engine revving and pulling away", 4.0),
    ("bullock_cart", "a bullock cart rolling on a dirt road with wooden wheels creaking", 6.0),
    ("train_pass", "a passenger train passing by with a horn", 8.0),
    ("footsteps_dirt", "footsteps walking on a dirt path", 4.0),
    ("footsteps_running", "footsteps running fast on a dirt path", 4.0),
    ("door_creak", "an old wooden door creaking open", 3.0),
    ("door_slam", "a heavy wooden door slamming shut", 2.0),
    ("monkey_chatter", "monkeys chattering and calling in trees", 5.0),
    ("dog_bark", "a village dog barking a few times", 3.0),
    ("cow_moo", "a cow mooing in a village", 3.0),
]


def sfx_price(count: int, per_sound: float) -> float:
    return round(count * per_sound, 2)


class SfxRefused(RuntimeError):
    """ElevenLabs refused the account (a bad key, no credits left): no further sound is asked for."""


def make_sfx(text: str, seconds: float, key: str, http=None) -> bytes:
    """One sound effect from ElevenLabs (text to sound effects), as MP3 bytes. Its length is left to ElevenLabs, so
    each costs the same (a flat 100 credits); the wanted length is said in the text. `http` defaults to requests."""
    import requests
    resp = (http or requests).post("https://api.elevenlabs.io/v1/sound-generation",
                                   headers={"xi-api-key": key, "Content-Type": "application/json",
                                            "Accept": "audio/mpeg"},
                                   json={"text": f"{text}, about {seconds:g} seconds", "prompt_influence": 0.5},
                                   timeout=120)
    status = getattr(resp, "status_code", 200)
    if status >= 400:
        body = str(getattr(resp, "text", ""))[:200]
        error = SfxRefused if status in (401, 402) or "quota" in body.lower() else RuntimeError
        raise error(f"ElevenLabs sound effects {status}: {body}")
    return resp.content


def build_sfx(pack: list[tuple[str, str, float]], ledger, per_sound: float, key: str, http=None, log=print,
              work: Path | None = None, root: Path | None = None) -> list[str]:
    """The pack's sounds the feed does not have yet into it (sfx/<name>_sfx_<when>.mp3), each checked against the
    approved amount (`ledger`) before it is asked for. Spend is recorded for every sound made, and for every call
    that timed out or lost its connection after it was sent (it may have been billed: noted as unconfirmed); a
    refused reply is not billed. A bad key or an account out of credits stops the build."""
    import tempfile

    import requests
    work = Path(work or tempfile.mkdtemp())
    work.mkdir(parents=True, exist_ok=True)
    have = {e.get("mood") for e in entries(SFX, root)}
    made = []
    for name, text, seconds in pack:
        if name in have:
            continue                                    # made by an earlier build: never paid for twice
        try:
            ledger.check(per_sound, f"sound effect {name}")
        except Exception as exc:
            log(f"  {name}: not made ({exc})")
            break                                       # the approved amount is spent: stop
        try:
            data = make_sfx(text, seconds, key, http)
        except SfxRefused as exc:
            log(f"  {name}: {exc}; stopping")
            break
        except (requests.Timeout, requests.ConnectionError) as exc:
            ledger.record("elevenlabs_sfx", name, per_sound, note=f"unconfirmed: may have been billed ({type(exc).__name__})")
            log(f"  {name}: no reply ({type(exc).__name__}); counted as spent")
            continue
        except Exception as exc:
            log(f"  {name}: not made ({type(exc).__name__}: {str(exc)[:120]})")
            continue
        try:
            ledger.record("elevenlabs_sfx", name, per_sound, seconds=seconds)
            raw = work / f"{name}.mp3"
            raw.write_bytes(data)
            stored = add(raw, SFX, name, {"prompt": text, "seconds": seconds, "made_for": {"pack": "starter"},
                                          "model": "elevenlabs sound-generation"}, root)
        except Exception as exc:
            log(f"  {name}: not kept ({type(exc).__name__}: {str(exc)[:120]})")
            continue
        made.append(stored.name)
        log(f"  {stored.name}")
    return made



def under_way(result) -> bool:
    """Whether a build's result says it started and has not ended (finished, or stopped with an error) in the last
    two hours (a longer one was killed: asking again is allowed)."""
    if not isinstance(result, dict) or not result.get("started") or result.get("finished") or result.get("error"):
        return False
    try:
        t = datetime.datetime.fromisoformat(result["started"])
    except ValueError:
        return False
    t = t if t.tzinfo else t.replace(tzinfo=datetime.timezone.utc)
    return (datetime.datetime.now(datetime.timezone.utc) - t).total_seconds() < 2 * 3600


def missing_sfx(have: set[str]) -> list[tuple[str, str, float]]:
    """The starter pack's sounds the feed does not have yet."""
    return [s for s in STARTER_SFX if s[0] not in have]
