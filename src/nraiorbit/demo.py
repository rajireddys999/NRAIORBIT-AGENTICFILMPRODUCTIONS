"""Offline demo: a 3-minute story ("Godavari Simham") with recorded agent outputs.

`nraiorbit demo <slug>` runs the full pipeline with these fixtures and the mock
providers, producing an animatic movie, trailer and subtitles without an API key or
GPU. The same fixtures drive the end-to-end test.
"""

from __future__ import annotations

import re

from .llm import ReplayLLM

DEMO_PLOT = """\
On the banks of the Godavari, a ruthless boss named Bhairava taxes the fishermen and
burns the boats of anyone who refuses. Arjun, a quiet young man who returned to the
village, stands up to him. Meera, a school teacher who loves Arjun, fears for his life.
At the interval Bhairava discovers Arjun is the son of Raghavaiah, the man he murdered
years ago. After Bhairava burns the fishing boats, the village rallies behind Arjun for a
final confrontation on the riverbank at night.
"""

DEMO_CONFIG = {
    "film": {"runtime_minutes": 3, "runtime_tolerance_minutes": 0.5, "trailer_seconds": 30, "teaser_seconds": 15},
    "review_gates": {g: {"enabled": True, "auto_approve": True}
                     for g in ("story", "cast", "pacing", "final_cut", "trailer")},
    "animatic": {"resolution": [640, 360]},
}

_CHARACTERS = [
    {
        "id": "arjun", "name_te": "అర్జున్", "name_en": "Arjun", "role": "hero",
        "physical": {"gender": "male", "age": 32, "height_cm": 180, "build": "athletic",
                     "skin_tone": "medium brown", "face": "strong jaw, thick eyebrows, calm eyes",
                     "hair": "short black, light stubble", "marks": "small scar above left eyebrow"},
        "costume_baseline": "cotton shirt with sleeves rolled, veshti",
        "personality": "quiet, patient, explosive when his people are threatened",
        "arc": "from hiding his past to leading the village",
        "voice": {"pitch": "low", "timbre": "warm, resonant", "dialect": "godavari", "speech_register": "colloquial",
                  "code_mix": "light", "speech_traits": ["few words", "pauses before threats"]},
    },
    {
        "id": "meera", "name_te": "మీరా", "name_en": "Meera", "role": "heroine",
        "physical": {"gender": "female", "age": 26, "height_cm": 162, "build": "slim",
                     "skin_tone": "wheatish", "face": "expressive eyes, round face",
                     "hair": "long black braid", "marks": "none"},
        "costume_baseline": "cotton saree, simple jewellery",
        "personality": "warm, brave, outspoken",
        "arc": "from fear to standing beside Arjun",
        "voice": {"pitch": "mid", "timbre": "clear, bright", "dialect": "godavari", "speech_register": "colloquial",
                  "code_mix": "light", "speech_traits": ["teasing", "asks direct questions"]},
    },
    {
        "id": "bhairava", "name_te": "భైరవ", "name_en": "Bhairava", "role": "villain",
        "physical": {"gender": "male", "age": 55, "height_cm": 176, "build": "heavy-set",
                     "skin_tone": "dark brown", "face": "thick moustache, heavy brow, cold stare",
                     "hair": "grey, oiled back", "marks": "gold tooth"},
        "costume_baseline": "white shirt and dhoti, gold chain",
        "personality": "arrogant, theatrical, cruel",
        "arc": "his empire collapses when the village stops fearing him",
        "voice": {"pitch": "low", "timbre": "gravelly", "dialect": "coastal_andhra", "speech_register": "rustic",
                  "code_mix": "none", "speech_traits": ["slow menace", "speaks of himself as the law"]},
    },
]


def _beat(i, summary, rasa, intensity, chars, set_piece="none"):
    return {"id": f"bt{i:02d}", "summary": summary, "rasa": rasa, "intensity": intensity,
            "characters": chars, "set_piece": set_piece}


STORY_BIBLE = {
    "title_te": "గోదావరి సింహం",
    "title_en": "Godavari Simham",
    "logline": "A quiet man must face the river boss who killed his father to free his village from fear.",
    "genres": ["action", "drama"],
    "tone": "mass action with family sentiment",
    "structure_template": "telugu_commercial",
    "era_and_region": "present-day Godavari delta village, Andhra Pradesh",
    "world": "Fishing village on the Godavari; boats, river ghats, a market, Bhairava's mansion.",
    "themes": ["fear vs courage", "a village's dignity", "a son's debt to his father"],
    "characters": _CHARACTERS,
    "acts": [
        {"name": "Act 1", "purpose": "world, hero intro, romance", "target_minutes": 1.5, "beats": [
            _beat(1, "Bhairava's men slash a fisherman's net", "bhayanaka", 5, ["bhairava"]),
            _beat(2, "Arjun stops the goons in the market", "veera", 8, ["arjun"], "hero_intro"),
            _beat(3, "Arjun and Meera at the river ghat", "shringara", 4, ["arjun", "meera"]),
        ]},
        {"name": "Interval", "purpose": "the twist", "target_minutes": 0.5, "beats": [
            _beat(4, "Bhairava learns Arjun is Raghavaiah's son", "raudra", 9, ["bhairava"], "interval_bang"),
        ]},
        {"name": "Act 2", "purpose": "loss and climax", "target_minutes": 1.0, "beats": [
            _beat(5, "Boats burnt; Meera consoles Arjun", "karuna", 7, ["arjun", "meera"], "emotional_peak"),
            _beat(6, "Night fight on the riverbank", "veera", 10, ["arjun", "bhairava"], "climax"),
        ]},
    ],
}


def _scene(i, heading, loc, tod, chars, synopsis, rasa, intensity, set_piece, seconds, beat):
    return {"scene_id": f"sc{i:03d}", "beat_ids": [beat], "heading": heading, "location_hint": loc,
            "time_of_day": tod, "characters": chars, "synopsis": synopsis, "rasa_primary": rasa,
            "rasa_secondary": None, "intensity": intensity, "set_piece": set_piece, "target_seconds": seconds}


OUTLINE = {"scenes": [
    _scene(1, "EXT. GODAVARI RIVERBANK - DAWN", "godavari_village_riverbank", "dawn", ["bhairava"],
           "Bhairava's men slash a fisherman's net and demand tribute.", "bhayanaka", 5, "none", 25, "bt01"),
    _scene(2, "EXT. VILLAGE MARKET - MORNING", "village_market", "morning", ["arjun"],
           "Arjun arrives and stops the goons.", "veera", 8, "hero_intro", 35, "bt02"),
    _scene(3, "EXT. RIVER GHAT - GOLDEN HOUR", "river_ghat", "golden_hour", ["arjun", "meera"],
           "Meera floats a lamp; she fears for Arjun.", "shringara", 4, "none", 30, "bt03"),
    _scene(4, "INT. BHAIRAVA'S MANSION - NIGHT", "bhairava_mansion", "night", ["bhairava"],
           "An old photo reveals Arjun is Raghavaiah's son.", "raudra", 9, "interval_bang", 30, "bt04"),
    _scene(5, "EXT. BURNT BOATS - DUSK", "godavari_village_riverbank", "dusk", ["arjun", "meera"],
           "Arjun sits among burnt boats; Meera kneels beside him.", "karuna", 7, "emotional_peak", 30, "bt05"),
    _scene(6, "EXT. RIVERBANK - NIGHT", "godavari_village_riverbank", "night", ["arjun", "bhairava"],
           "Torch-lit final fight; Arjun throws Bhairava into the river.", "veera", 10, "climax", 30, "bt06"),
]}


def _action(sid, n, en):
    return {"block_id": f"{sid}_b{n:03d}", "kind": "action", "character_id": None, "text_te": en,
            "text_en": en, "delivery": None}


def _line(sid, n, who, te, en, rasa, intensity, pace="medium", volume="normal", notes=""):
    return {"block_id": f"{sid}_b{n:03d}", "kind": "dialogue", "character_id": who, "text_te": te,
            "text_en": en, "delivery": {"rasa": rasa, "intensity": intensity, "pace": pace,
                                        "volume": volume, "notes": notes}}


SCENES = {
    "sc001": [
        _action("sc001", 1, "Dawn mist over the Godavari. Bhairava's men drag a fisherman's net out and slash it."),
        _line("sc001", 2, "bhairava", "ఈ గోదావరి ఒడ్డున నా మాటే శాసనం. చేపలు కావాలంటే, నాకు కప్పం కట్టాలి.",
              "On these Godavari banks, my word is law. Want fish? Pay me tribute.", "raudra", 6, "slow", "low",
              "slow menace"),
    ],
    "sc002": [
        _action("sc002", 1, "The market freezes. A motorcycle rolls in through the dust. Arjun steps off, calm."),
        _line("sc002", 2, "arjun", "ఈ ఊరు నాది. ఈ జనం నావాళ్ళు. ఎవడైనా వాళ్ళ మీద చెయ్యి వేస్తే... ఆ చెయ్యి ఉండదు.",
              "This village is mine. These people are mine. Raise a hand against them... and lose that hand.",
              "veera", 8, "slow", "low", "quiet threat, not shouting"),
        _action("sc002", 3, "Three goons charge. Arjun drops them in one slow-motion move."),
    ],
    "sc003": [
        _action("sc003", 1, "Golden hour on the river steps. Meera floats a lamp on the water; Arjun watches."),
        _line("sc003", 2, "meera", "నువ్వు ఎప్పుడూ గొడవల్లోనే ఉంటావా? నాకు భయంగా ఉంది, అర్జున్.",
              "Are you always going to be in fights? I'm scared, Arjun.", "shringara", 4, "medium", "normal",
              "teasing first, then honest"),
        _line("sc003", 3, "arjun", "భయపడకు. నేనున్నాను కదా.", "Don't be afraid. I'm here, aren't I?",
              "shringara", 4, "slow", "low", "gentle smile"),
    ],
    "sc004": [
        _action("sc004", 1, "Bhairava's mansion. A henchman lays an old photograph down: Arjun as a boy, beside his father."),
        _line("sc004", 2, "bhairava", "వాడు... ఆ రాఘవయ్య కొడుకా? అయితే ఈ యుద్ధం ఇప్పుడే మొదలైంది.",
              "He's... Raghavaiah's son? Then this war has only just begun.", "raudra", 9, "slow", "loud",
              "disbelief turning to rage"),
    ],
    "sc005": [
        _action("sc005", 1, "Burnt fishing boats smoulder at dusk. Arjun sits in the ash. Meera kneels beside him."),
        _line("sc005", 2, "meera", "నువ్వు ఒక్కడివే కాదు. మేమంతా నీతోనే ఉన్నాం.",
              "You are not alone. All of us are with you.", "karuna", 7, "slow", "low", "holding back tears"),
    ],
    "sc006": [
        _action("sc006", 1, "Torches on the riverbank. Arjun faces Bhairava; the villagers stand behind Arjun."),
        _line("sc006", 2, "arjun", "నా నాన్న ప్రాణం తీశావు. ఈ రోజు నీ అహంకారం తీస్తాను.",
              "You took my father's life. Today I take your arrogance.", "veera", 10, "slow", "normal",
              "the mass dialogue: measured, every word lands"),
        _action("sc006", 3, "The final fight. Arjun throws Bhairava into the shallow river."),
    ],
}


def _shot(sid, n, blocks, size, move, chars, costumes, rasa, intensity, dur, prompt, motion,
          fps="normal", sfx=(), action=None, trailer=False, punch=False):
    return {"shot_id": f"{sid}_s{n:03d}", "block_ids": [f"{sid}_b{b:03d}" for b in blocks], "size": size,
            "camera_move": move, "lens_mm": 35 if size in ("WS", "EWS") else 50, "fps_effect": fps,
            "characters_in_frame": chars, "costume_ids": costumes, "action_template_id": action,
            "image_prompt": prompt, "motion_prompt": motion, "duration_s": dur, "rasa": rasa,
            "intensity": intensity, "sfx_cues": list(sfx), "trailer_candidate": trailer, "punch_dialogue": punch}


SHOTS = {
    "sc001": [
        _shot("sc001", 1, [1], "EWS", "drone", [], [], "bhayanaka", 5, 8,
              "Misty dawn over the Godavari, fishing boats, men on the bank", "slow drone glide over the river",
              trailer=True),
        _shot("sc001", 2, [1], "MS", "handheld", [], [], "bhayanaka", 6, 6,
              "Henchmen slash a fishing net on the bank", "knife cuts the net, fisherman pulls back",
              sfx=["net_tear"]),
        _shot("sc001", 3, [2], "CU", "push_in", ["bhairava"], ["cst_bhairava_white"], "raudra", 6, 11,
              "bhairava in white, gold chain, staring down, low angle", "slow push in as he speaks",
              trailer=True, punch=True),
    ],
    "sc002": [
        _shot("sc002", 1, [1], "WS", "track", ["arjun"], ["cst_arjun_village"], "veera", 7, 8,
              "Dusty village market, a motorcycle rolling in, crowd parting", "tracking alongside the bike",
              fps="slowmo", action="hero_slowmo_entry", trailer=True, sfx=["bike_rev"]),
        _shot("sc002", 2, [2], "MCU", "push_in", ["arjun"], ["cst_arjun_village"], "veera", 8, 12,
              "arjun low angle, sunlight behind, dust in the air", "slow push in, he lifts his eyes",
              trailer=True, punch=True),
        _shot("sc002", 3, [3], "MS", "orbit", ["arjun"], ["cst_arjun_village"], "veera", 9, 15,
              "arjun mid-kick, three goons flying back, market stalls", "360 orbit with speed ramp on impact",
              fps="speed_ramp", action="punch_combo_3hit", trailer=True, sfx=["punch_heavy", "crowd_cheer_te"]),
    ],
    "sc003": [
        _shot("sc003", 1, [1], "WS", "static", ["meera", "arjun"], ["cst_meera_saree", "cst_arjun_village"],
              "shringara", 3, 9, "River ghat at golden hour, meera floating an oil lamp, arjun on the steps",
              "lamp drifts on the water", trailer=True),
        _shot("sc003", 2, [2], "CU", "push_in", ["meera"], ["cst_meera_saree"], "shringara", 4, 11,
              "meera close-up, warm backlight, worried smile", "gentle push in"),
        _shot("sc003", 3, [3], "CU", "static", ["arjun"], ["cst_arjun_village"], "shringara", 4, 10,
              "arjun close-up, soft smile, golden light", "he glances at her"),
    ],
    "sc004": [
        _shot("sc004", 1, [1], "insert", "push_in", [], [], "raudra", 8, 10,
              "An old photograph on a wooden table: a boy beside his father", "slow push into the photo",
              sfx=["paper_slide"]),
        _shot("sc004", 2, [2], "ECU", "crash_zoom", ["bhairava"], ["cst_bhairava_white"], "raudra", 9, 20,
              "bhairava's eyes widening in lamplight", "crash zoom as rage builds", trailer=True, punch=True),
    ],
    "sc005": [
        _shot("sc005", 1, [1], "WS", "static", ["arjun"], ["cst_arjun_village"], "karuna", 6, 12,
              "Burnt boats smouldering at dusk, arjun sitting in the ash", "smoke drifts", trailer=True),
        _shot("sc005", 2, [2], "two_shot", "push_in", ["meera", "arjun"], ["cst_meera_saree", "cst_arjun_village"],
              "karuna", 7, 18, "meera kneeling beside arjun, her hand on his shoulder, dusk light",
              "very slow push in"),
    ],
    "sc006": [
        _shot("sc006", 1, [1], "WS", "crane_up", ["arjun", "bhairava"], ["cst_arjun_climax", "cst_bhairava_white"],
              "veera", 9, 8, "Torch-lit riverbank at night, two men facing off, villagers behind",
              "crane up to reveal the crowd", trailer=True),
        _shot("sc006", 2, [2], "MCU", "push_in", ["arjun"], ["cst_arjun_climax"], "veera", 10, 10,
              "arjun in torchlight, torn shirt, steady eyes", "push in", trailer=True, punch=True),
        _shot("sc006", 3, [3], "MS", "handheld", ["arjun", "bhairava"], ["cst_arjun_climax", "cst_bhairava_white"],
              "veera", 10, 12, "arjun throwing bhairava into the shallow river, spray in torchlight",
              "impact with slow-motion splash", fps="speed_ramp", action="punch_combo_3hit",
              sfx=["punch_heavy", "water_splash"]),
    ],
}

CUE_REVISIONS = {"revisions": [{
    "cue_id": "cue005", "raga": "subhapantuvarali", "tala": None, "bpm": None, "instruments": None,
    "leitmotif_character": "arjun", "silence_before_s": None,
    "reason": "State the hero theme in a pathos raga at his lowest point.",
}]}


def _scene_id(task: str) -> str:
    m = re.search(r"sc\d{3}", task)
    if not m:
        raise ValueError(f"no scene id in task: {task[:80]}")
    return m.group(0)


def demo_llm() -> ReplayLLM:
    return ReplayLLM({
        "StoryBible": STORY_BIBLE,
        "Outline": OUTLINE,
        "SceneScript": lambda task: {
            "scene_id": (sid := _scene_id(task)),
            "heading": next(s["heading"] for s in OUTLINE["scenes"] if s["scene_id"] == sid),
            "synopsis": next(s["synopsis"] for s in OUTLINE["scenes"] if s["scene_id"] == sid),
            "rasa_primary": next(s["rasa_primary"] for s in OUTLINE["scenes"] if s["scene_id"] == sid),
            "intensity": next(s["intensity"] for s in OUTLINE["scenes"] if s["scene_id"] == sid),
            "blocks": SCENES[sid],
        },
        "Critique": {"score": 9, "issues": [], "revision_instructions": []},
        "ShotList": lambda task: {"scene_id": (sid := _scene_id(task)), "shots": SHOTS[sid]},
        "CueRevisions": CUE_REVISIONS,
    })
