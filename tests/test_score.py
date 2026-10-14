"""Score to picture: style presets and timed sections for generated music."""

import re

import pytest

from nraiorbit.planning.score import load_styles, peak_offset, score_cue, slot_for, timed_structure
from nraiorbit.providers.lyria import cue_prompt
from nraiorbit.schemas import Cue, Shot

STYLES = {"music": {"styles": {"elevation": "folk_mass", "emotion": "violin_swell"}}}


def cue(**kw):
    base = dict(cue_id="cue002", scene_ids=["sc002"], start_s=25.0, duration_s=40.0, rasa="veera", intensity=8,
                cue_type="hero_elevation", raga="nattai", tala="adi", bpm=141, instruments=["thavil", "dappu"],
                leitmotif_character=None, peak=True, silence_before_s=1.5)
    return Cue(**{**base, **kw})


def shot(sid, intensity, punch=False):          # only the fields the scorer reads
    return Shot.model_construct(shot_id=sid, intensity=intensity, punch_dialogue=punch, trailer_candidate=False)


def seconds(mark):
    m, s = mark.split(":")
    return int(m) * 60 + int(s)


def test_slots():
    assert slot_for(cue()) == "elevation" and slot_for(cue(peak=False, intensity=6)) == "action"
    assert slot_for(cue(rasa="raudra")) == "villain" and slot_for(cue(rasa="karuna")) == "emotion"
    assert slot_for(cue(cue_type="trailer")) == "trailer"


def test_styles_pick_presets_and_trailer_follows_elevation():
    styles = load_styles(STYLES)
    assert styles["elevation"]["name"] == "folk_mass" and styles["trailer"]["name"] == "folk_mass"
    assert styles["emotion"]["shape"] == "emotion"
    assert load_styles({}) == {}
    with pytest.raises(KeyError, match="unknown preset"):
        load_styles({"music": {"styles": {"emotion": "nope"}}})


def test_elevation_hit_lands_on_the_strongest_shot():
    spans = [(26.5, shot("a", 5)), (40.0, shot("b", 9, punch=True)), (48.0, shot("c", 9)), (70.0, shot("d", 10))]
    c = cue()
    assert peak_offset(c, spans) == pytest.approx(13.5)          # 40.0 - (25 + 1.5); "d" is outside the cue
    notes = score_cue(c, spans, load_styles(STYLES)).notes
    marks = dict((name, (seconds(a), seconds(b))) for a, b, name in re.findall(r"\[(\d+:\d\d) - (\d+:\d\d)\] (\w+):", notes))
    assert list(marks) == ["Intro", "Build", "Drop", "Hit", "Drive", "Outro"]
    assert marks["Hit"][0] in (13, 14) and marks["Drop"][1] == marks["Hit"][0]
    assert marks["Outro"][1] == round(c.duration_s - c.silence_before_s)
    assert "conch" in notes and "Composer" not in notes and "  " not in notes


def test_emotion_peaks_late_and_releases():
    c = cue(cue_id="cue007", rasa="karuna", peak=False, duration_s=60.0, silence_before_s=0.0)
    notes = timed_structure(c, load_styles(STYLES)["emotion"], 48.0)
    names = re.findall(r"\] (\w+):", notes)
    assert names == ["Opening", "Build", "Breath", "Peak", "Release"] and "[0:48 - " in notes


def test_cue_with_notes_or_without_preset_is_unchanged():
    styles = load_styles(STYLES)
    trailer = cue(cue_type="trailer", notes="0.0-5.0s world: calm")
    assert score_cue(trailer, [], styles).notes == "0.0-5.0s world: calm"
    calm = cue(rasa="shanta", peak=False)
    assert score_cue(calm, [], styles).notes == ""


def test_prompt_uses_the_preset_style_line():
    styles = load_styles({"music": {"styles": {"emotion": "alaap_choir"}}})
    c = cue(cue_id="cue007", rasa="karuna", peak=False, instruments=["strings"], notes="[0:00 - 0:20] Opening: x.")
    p = cue_prompt(c, style=styles["emotion"])
    assert "Style: Emotional Indian cinematic score: a wordless female classical alaap" in p
    assert p.startswith("Telugu film background score (BGM). Wordless vocals only")        # not "instrumental"
    assert "wordless textures" in p and "Structure (follow these timings)" in p and "Avoid: lyrics" in p
    villain = cue_prompt(cue(rasa="raudra"))
    assert "Villain menace" in villain and "hero style" not in villain
