import numpy as np
import pytest

from nraiorbit.guards.identity import (
    FaceObservation, IdentityGuard, IdentityThresholds, assign_faces, build_centroid,
)
from nraiorbit.guards.voice import VoiceGuard, VoiceTake, VoiceThresholds, cer

RNG = np.random.default_rng(7)
DIM = 512


def unit(v):
    return v / np.linalg.norm(v)


def near(centroid, similarity):
    """A random unit vector with the given cosine similarity to `centroid`."""
    noise = unit(RNG.normal(size=centroid.shape[0]))
    noise = unit(noise - noise @ centroid * centroid)
    return unit(similarity * centroid + np.sqrt(1 - similarity ** 2) * noise)


def face(emb, size=200):
    return FaceObservation(embedding=emb, bbox=(0, 0, size, size))


@pytest.fixture
def cast():
    return {"arjun": unit(RNG.normal(size=DIM)), "meera": unit(RNG.normal(size=DIM))}


def test_centroid_is_normalised():
    c = build_centroid([RNG.normal(size=DIM) for _ in range(5)])
    assert np.isclose(np.linalg.norm(c), 1.0)


def test_same_identity_passes(cast):
    guard = IdentityGuard(cast)
    frames = [[face(near(cast["arjun"], 0.70))] for _ in range(8)]
    result = guard.check_clip(frames, ["arjun"], IdentityThresholds())
    assert result.passed, result.issues
    assert result.scores["arjun"].mean == pytest.approx(0.70, abs=0.01)


def test_wrong_face_fails(cast):
    guard = IdentityGuard(cast)
    stranger = unit(RNG.normal(size=DIM))
    frames = [[face(near(stranger, 0.9))] for _ in range(8)]
    result = guard.check_clip(frames, ["arjun"], IdentityThresholds())
    assert not result.passed
    assert any("mean similarity" in i for i in result.issues)


def test_identity_drift_detected(cast):
    guard = IdentityGuard(cast)
    sims = [0.80, 0.78, 0.60, 0.50, 0.75, 0.48, 0.79, 0.52]
    frames = [[face(near(cast["arjun"], s))] for s in sims]
    result = guard.check_clip(frames, ["arjun"], IdentityThresholds(min_mean_similarity=0.5))
    assert any("drift" in i for i in result.issues)


def test_two_shot_assignment_and_swap(cast):
    guard = IdentityGuard(cast)
    a, m = near(cast["arjun"], 0.7), near(cast["meera"], 0.7)
    ok = guard.check_clip([[face(m), face(a)]] * 4, ["arjun", "meera"], IdentityThresholds())
    assert ok.passed, ok.issues
    # both faces look like arjun: meera's slot gets an arjun-like face -> swap/bleed
    bleed = guard.check_clip([[face(a), face(near(cast["arjun"], 0.65))]] * 4, ["arjun", "meera"],
                             IdentityThresholds())
    assert not bleed.passed
    assert any("swap" in i or "mean similarity" in i for i in bleed.issues)


def test_missing_face_and_small_faces(cast):
    guard = IdentityGuard(cast)
    tiny = [[face(near(cast["arjun"], 0.8), size=20)] for _ in range(6)]
    result = guard.check_clip(tiny, ["arjun"], IdentityThresholds(min_face_px=48))
    assert any("missing" in i for i in result.issues)
    wide = guard.check_clip(tiny, ["arjun"], IdentityThresholds(min_face_px=48), faces_required=False)
    assert wide.passed


def test_unknown_character_needs_casting(cast):
    with pytest.raises(KeyError):
        IdentityGuard(cast).check_clip([[]], ["bhairava"], IdentityThresholds())


def test_assign_faces_is_optimal():
    sim = np.array([[0.9, 0.8], [0.85, 0.1]])
    # greedy would pick (0,0) then (1,1) = 1.0; optimal is (0,1)+(1,0) = 1.65
    assert sorted(assign_faces(sim)) == [(0, 1), (1, 0)]


def test_thresholds_per_shot_size():
    cfg = {"min_mean_similarity": {"CU": 0.55, "MS": 0.45}, "min_frame_similarity": 0.4}
    assert IdentityThresholds.for_shot(cfg, "CU").min_mean_similarity == 0.55
    assert IdentityThresholds.for_shot(cfg, "WS").min_mean_similarity == 0.45
    assert IdentityThresholds.for_shot(cfg, "CU", {"min_mean_similarity": 0.6}).min_mean_similarity == 0.6


def test_cer_counts_telugu_graphemes():
    ref = "భయపడకు. నేనున్నాను కదా."
    assert cer(ref, ref) == 0.0
    assert cer(ref, "భయపడకు నేనున్నాను కదా") == 0.0          # punctuation/spacing ignored
    one_off = cer(ref, "భయపడకు. నేనున్నాను కాదా.")             # one vowel sign changed
    assert 0 < one_off < 0.15
    assert cer(ref, "") == 1.0


def test_voice_guard_ranks_takes():
    centroid = unit(RNG.normal(size=192))
    guard = VoiceGuard({"arjun_voice": centroid}, VoiceThresholds())
    script = "భయపడకు. నేనున్నాను కదా."
    good = VoiceTake("t1", speaker_embedding=near(centroid, 0.85), asr_text=script,
                     emotion_top2=["shringara", "shanta"], mos=4.2, duration_s=2.0)
    wrong_voice = VoiceTake("t2", speaker_embedding=near(centroid, 0.3), asr_text=script, mos=4.5)
    results = [guard.check(t, voice_id="arjun_voice", script_te=script, target_rasa="shringara",
                           target_duration_s=2.1) for t in (good, wrong_voice)]
    assert results[0].passed and not results[1].passed
    assert guard.select_best(results).take_id == "t1"
