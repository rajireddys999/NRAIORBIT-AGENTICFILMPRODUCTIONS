"""5-minute POC: "Godavari Simham", written in Claude Code (no Claude API calls needed).

`nraiorbit poc <slug>` writes the story bible, outline, scene scripts, shot lists and cue
sheet into a project. `nraiorbit run <slug>` then skips those stages and goes straight
to casting, rendering and assembly, so the only bills are the generators (Veo, Google Cloud
lip-sync). Telugu dialogue should get a native-speaker review before recording.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from .agents.production import CastingAgent
from .config import REPO_ROOT, deep_merge, load_navarasa
from .library.registry import AssetRegistry
from .demo import _CHARACTERS, _action, _beat, _line, _scene, _shot
from .planning.music import apply_revisions, plan_cues
from .schemas import CueRevisions, Outline, SceneScript, ShotList, StoryBible

POC_PLOT = """\
On the banks of the Godavari, the river boss Bhairava taxes the fishermen and burns the
boats of anyone who refuses. Arjun, a quiet man who has come back to the village, stands
up to his men in the market and tells the fishermen to sell their own catch. Meera, the
village school teacher who loves him, fears for his life.

At the interval Bhairava discovers that Arjun is the son of Raghavaiah, the fishermen's
leader he killed twenty years ago. A flashback shows the night his father was dragged
away. Bhairava burns the boats; Meera holds Arjun together. On a torch-lit
night the whole village stands behind Arjun, and he throws Bhairava into the river.
At dawn the boats go back into the water.
"""

POC_CONFIG = {
    "film": {"runtime_minutes": 5, "runtime_tolerance_minutes": 0.5, "trailer_seconds": 45},
    # Automatic: Veo API for video, Gemini TTS for lines without a recording in inbox/dialogue.
    "providers": {"video": "veo", "tts": "inbox", "music": "inbox"},
    "voice": {"inbox_fallback": "gemini"},
    # Quality first: Veo 3.1 standard at native 1080p (8 s clips), Gemini's pro image model for faces/first frames.
    "google": {"veo_mode": "keyframe", "veo_model": "veo-3.1-generate-preview", "veo_tier": "standard",
               "veo_resolution": "1080p", "image_model": "gemini-3-pro-image"},
    "budget": {"max_usd": 250},
    # Spending is guarded by the preview + confirmation and the budget, so the POC runs straight through.
    "review_gates": {g: {"auto_approve": True} for g in ("story", "cast", "pacing", "final_cut", "trailer")},
}

POC_PRICING = {"claude": {"billing": "subscription"},
               "gemini": {"image_per_image": 0.134}}     # pro image model, 1K-2K (third-party estimate; verify)
MANUAL_OVERRIDES = {"providers": {"video": "shotpack"}, "voice": {"inbox_fallback": "mock"}}

STORY_BIBLE = {
    "title_te": "గోదావరి సింహం",
    "title_en": "Godavari Simham",
    "logline": "A quiet man returns to his Godavari village and must face the river boss who killed "
               "his father, to free the fishermen from fear.",
    "genres": ["action", "drama"],
    "tone": "mass action with family sentiment and a light romance",
    "structure_template": "telugu_commercial",
    "era_and_region": "present-day Godavari delta village, Andhra Pradesh",
    "world": "A fishing village on the Godavari: wooden boats, nets drying on the bank, river ghats "
             "with lamps, a weekly market, and Bhairava's old mansion above the village.",
    "themes": ["fear vs courage", "a village's dignity", "a son's debt to his father"],
    "characters": _CHARACTERS,
    "acts": [
        {"name": "Act 1", "purpose": "the world, the hero, the romance, the challenge", "target_minutes": 2.6, "beats": [
            _beat(1, "Bhairava's men slash nets and demand tribute", "bhayanaka", 5, ["bhairava"]),
            _beat(2, "Arjun stops Bhairava's goons in the market", "veera", 8, ["arjun"], "hero_intro"),
            _beat(3, "Meera and Arjun at the river ghat", "shringara", 4, ["arjun", "meera"]),
            _beat(4, "Arjun tells the fishermen to sell their own catch", "veera", 6, ["arjun"]),
            _beat(5, "Bhairava hears one man is turning the village", "raudra", 7, ["bhairava"]),
        ]},
        {"name": "Interval", "purpose": "the twist", "target_minutes": 0.4, "beats": [
            _beat(6, "An old photo: Arjun is Raghavaiah's son", "raudra", 9, ["bhairava"], "interval_bang"),
        ]},
        {"name": "Act 2", "purpose": "the wound, the low point, the climax", "target_minutes": 2.0, "beats": [
            _beat(7, "Flashback: the night Raghavaiah was taken", "karuna", 7, ["arjun"], "flashback"),
            _beat(8, "Burnt boats; Meera holds Arjun together", "karuna", 8, ["arjun", "meera"], "emotional_peak"),
            _beat(9, "Torch-lit riverbank: the village stands with Arjun", "veera", 10, ["arjun", "bhairava"],
                  "climax"),
            _beat(10, "Dawn: the boats return to the water", "shanta", 4, ["arjun", "meera"]),
        ]},
    ],
}

OUTLINE = {"scenes": [
    _scene(1, "EXT. GODAVARI RIVERBANK - DAWN", "godavari_village_riverbank", "dawn", ["bhairava"],
           "Bhairava's men slash a fisherman's net; Bhairava demands tribute.", "bhayanaka", 5, "none", 25, "bt01"),
    _scene(2, "EXT. VILLAGE MARKET - MORNING", "village_market", "morning", ["arjun"],
           "Goons wreck a vegetable cart; Arjun rides in and drops them.", "veera", 8, "hero_intro", 40, "bt02"),
    _scene(3, "EXT. RIVER GHAT - GOLDEN HOUR", "river_ghat", "golden_hour", ["arjun", "meera"],
           "Meera floats a lamp and says she is scared for him; he reassures her.", "shringara", 4, "none", 30, "bt03"),
    _scene(4, "EXT. FISHING BOATS - NOON", "godavari_village_riverbank", "noon", ["arjun"],
           "Arjun tells the fishermen to sell their own catch; they cheer.", "veera", 6, "none", 30, "bt04"),
    _scene(5, "INT. BHAIRAVA'S MANSION - NIGHT", "bhairava_mansion", "night", ["bhairava"],
           "A bruised henchman reports; Bhairava smashes a glass.", "raudra", 7, "none", 30, "bt05"),
    _scene(6, "INT. BHAIRAVA'S MANSION - NIGHT", "bhairava_mansion", "night", ["bhairava"],
           "An old photograph: Arjun is Raghavaiah's son. INTERVAL.", "raudra", 9, "interval_bang", 25, "bt06"),
    _scene(7, "EXT. RIVERBANK - NIGHT (20 YEARS AGO)", "godavari_village_riverbank", "night", ["arjun"],
           "Rain; Raghavaiah is dragged away into the dark. Arjun's voice-over.", "karuna", 7, "flashback", 30, "bt07"),
    _scene(8, "EXT. BURNT BOATS - DUSK", "godavari_village_riverbank", "dusk", ["arjun", "meera"],
           "The boats are burnt; Meera kneels beside Arjun; he resolves to finish it.", "karuna", 8,
           "emotional_peak", 30, "bt08"),
    _scene(9, "EXT. RIVERBANK - NIGHT", "godavari_village_riverbank", "night", ["arjun", "bhairava"],
           "Torches; the village stands behind Arjun; he throws Bhairava into the river.", "veera", 10,
           "climax", 45, "bt09"),
    _scene(10, "EXT. RIVERBANK - DAWN", "godavari_village_riverbank", "dawn", ["arjun", "meera"],
           "Dawn; boats go back into the water; title.", "shanta", 4, "none", 15, "bt10"),
]}

A, M, B = "arjun", "meera", "bhairava"
AV, AC, MS, BW = "cst_arjun_village", "cst_arjun_climax", "cst_meera_saree", "cst_bhairava_white"

SCENES = {
    "sc001": [
        _action("sc001", 1, "Dawn mist over the Godavari. Bhairava's men drag a fisherman's net out of the water and slash it."),
        _action("sc001", 2, "A white jeep stops on the bank. Bhairava steps down, gold chain catching the light."),
        _line("sc001", 3, B, "ఈ గోదావరి ఒడ్డున నా మాటే శాసనం. చేపలు కావాలంటే, నాకు కప్పం కట్టాలి.",
              "On these Godavari banks, my word is law. Want fish? Pay me tribute.", "raudra", 6, "slow", "low",
              "slow menace, almost bored"),
    ],
    "sc002": [
        _action("sc002", 1, "Market morning. Two goons kick over a vegetable cart; tomatoes roll in the dust."),
        _action("sc002", 2, "A motorcycle engine. The crowd turns. Arjun rides in through the dust and steps off."),
        _line("sc002", 3, A, "ఈ ఊరు నాది. ఈ జనం నావాళ్ళు. ఎవడైనా వాళ్ళ మీద చెయ్యి వేస్తే... ఆ చెయ్యి ఉండదు.",
              "This village is mine. These people are mine. Raise a hand against them... and lose that hand.",
              "veera", 8, "slow", "low", "quiet threat, not shouting"),
        _action("sc002", 4, "Three goons charge. Arjun drops them in one spinning move; they crash into the stalls."),
        _line("sc002", 5, A, "వెళ్ళి మీ భైరవకి చెప్పు. అర్జున్ వచ్చాడని.",
              "Go tell your Bhairava. Arjun is back.", "veera", 8, "slow", "normal", "to the goon on the ground"),
    ],
    "sc003": [
        _action("sc003", 1, "Golden hour on the river steps. Meera floats an oil lamp on the water; Arjun sits on the steps."),
        _line("sc003", 2, M, "నువ్వు ఎప్పుడూ గొడవల్లోనే ఉంటావా? నాకు భయంగా ఉంది, అర్జున్.",
              "Are you always going to be in fights? I'm scared, Arjun.", "shringara", 4, "medium", "normal",
              "teasing first, then honest"),
        _line("sc003", 3, A, "భయపడకు. నేనున్నాను కదా.", "Don't be afraid. I'm here, aren't I?",
              "shringara", 4, "slow", "low", "gentle smile"),
        _action("sc003", 4, "She splashes river water at him. He laughs for the first time."),
    ],
    "sc004": [
        _action("sc004", 1, "Noon. Fishermen load baskets of fish into boats bound for the town market. "
                            "Arjun pushes a boat into the water."),
        _line("sc004", 2, A, "ఇక నుంచి మన చేపలు మనమే అమ్ముకుందాం. ఎవడికీ కప్పం కట్టేది లేదు.",
              "From now on we sell our own fish. No more tribute to anyone.", "veera", 6, "medium", "loud",
              "rallying the men"),
        _action("sc004", 3, "The fishermen cheer and raise their oars. The boats sail out together."),
    ],
    "sc005": [
        _action("sc005", 1, "Bhairava's mansion at night. A bruised henchman kneels and reports. "
                            "Bhairava turns a gold ring on his finger."),
        _line("sc005", 2, B, "ఒక్కడు... ఒక్కడు నా ఊరిని నా మీదకే తిప్పుతున్నాడా?",
              "One man... one man is turning my own village against me?", "raudra", 7, "slow", "low",
              "disbelief, then cold"),
        _action("sc005", 3, "He crushes the glass in his hand and lets it fall to the floor."),
    ],
    "sc006": [
        _action("sc006", 1, "The henchman slides an old photograph onto the table: Raghavaiah by his boat, "
                            "with the same eyes and the same scar above the left eyebrow as Arjun."),
        _line("sc006", 2, B, "వాడు... ఆ రాఘవయ్య కొడుకా? అయితే ఈ యుద్ధం ఇప్పుడే మొదలైంది.",
              "He's... Raghavaiah's son? Then this war has only just begun.", "raudra", 9, "slow", "loud",
              "disbelief turning to rage"),
        _action("sc006", 3, "Thunder over the mansion. INTERVAL."),
    ],
    "sc007": [
        _action("sc007", 1, "Twenty years ago. Rain and torches on the riverbank. Men drag Raghavaiah away into the dark."),
        _action("sc007", 2, "A hand-carved toy boat floats away on the dark water. A broken oar lies in the mud."),
        _line("sc007", 3, A, "ఆ రాత్రి నేను ఏడవలేదు. గుర్తుపెట్టుకున్నాను.",
              "That night I didn't cry. I remembered.", "karuna", 7, "very_slow", "low", "voice-over, flat and heavy"),
    ],
    "sc008": [
        _action("sc008", 1, "Dusk. The boats are burnt and smouldering. Arjun sits in the ash holding a burnt oar."),
        _line("sc008", 2, M, "నువ్వు ఒక్కడివే కాదు. మేమంతా నీతోనే ఉన్నాం.",
              "You are not alone. All of us are with you.", "karuna", 7, "slow", "low", "holding back tears"),
        _line("sc008", 3, A, "నాన్న కోసం కాదు మీరా... ఈ ఊరి కోసం. ఈసారి వాడు తప్పించుకోలేడు.",
              "Not for my father, Meera... for this village. This time he won't escape.", "veera", 8, "slow", "low",
              "grief hardening into resolve"),
        _action("sc008", 4, "His hand tightens on the burnt oar."),
    ],
    "sc009": [
        _action("sc009", 1, "Night. Torches on the riverbank. Bhairava's men line up. Behind Arjun, the whole village walks in."),
        _line("sc009", 2, B, "ఈ ఊరు నాకు భయపడుతుంది, అర్జున్.", "This village fears me, Arjun.", "raudra", 8, "slow",
              "normal", "smiling, certain"),
        _line("sc009", 3, A, "నా నాన్న ప్రాణం తీశావు. ఈ రోజు నీ అహంకారం తీస్తాను.",
              "You took my father's life. Today I take your arrogance.", "veera", 10, "slow", "normal",
              "the mass dialogue: measured, every word lands"),
        _action("sc009", 4, "Arjun breaks through the henchmen, grapples with Bhairava and throws him into the shallow river."),
    ],
    "sc010": [
        _action("sc010", 1, "Dawn. The boats go back into the water. Meera and Arjun watch the fishermen head out, singing."),
        _action("sc010", 2, "Title card: Godavari Simham."),
    ],
}

SHOTS = {
    "sc001": [
        _shot("sc001", 1, [1], "EWS", "drone", [], [], "bhayanaka", 5, 6,
              "Misty dawn over the wide Godavari river, wooden fishing boats, palm trees, men on the bank",
              "slow drone glide low over the water", trailer=True),
        _shot("sc001", 2, [1], "MS", "handheld", [], [], "bhayanaka", 6, 4,
              "Two henchmen in dark shirts slash a fishing net on the riverbank with knives",
              "knife cuts the net, a fisherman pulls back", sfx=["net_tear"]),
        _shot("sc001", 3, [1], "CU", "static", [], [], "karuna", 5, 4,
              "An old fisherman with a grey beard, folded hands, pleading, dawn light",
              "his lips tremble, he lowers his eyes"),
        _shot("sc001", 4, [2], "MCU", "crane_down", [B], [BW], "raudra", 6, 5,
              "bhairava stepping down from a white jeep, low angle, gold chain glinting, mist behind",
              "camera cranes down as his feet touch the ground", sfx=["jeep_door"]),
        _shot("sc001", 5, [3], "CU", "push_in", [B], [BW], "raudra", 6, 6,
              "bhairava close-up, thick moustache, cold stare, low angle, dawn rim light",
              "slow push in as he speaks", trailer=True, punch=True),
    ],
    "sc002": [
        _shot("sc002", 1, [1], "WS", "handheld", [], [], "raudra", 6, 4,
              "Busy village market, two goons kick over a vegetable cart, a woman vendor falls back",
              "cart tips over, vegetables scatter", sfx=["cart_crash"]),
        _shot("sc002", 2, [1], "insert", "static", [], [], "raudra", 5, 3,
              "Red tomatoes rolling through dust on a market lane", "tomatoes roll and stop"),
        _shot("sc002", 3, [2], "WS", "track", [A], [AV], "veera", 7, 6,
              "arjun riding a motorcycle into the market through dust, crowd parting, morning sun behind",
              "tracking alongside the bike in slow motion", fps="slowmo", action="hero_slowmo_entry",
              trailer=True, sfx=["bike_rev"]),
        _shot("sc002", 4, [2], "insert", "static", [], [], "veera", 7, 3,
              "A man's sandalled foot stepping down into the dust beside a motorcycle", "dust puffs up in slow motion",
              fps="slowmo", sfx=["footsteps_dust"]),
        _shot("sc002", 5, [3], "MCU", "push_in", [A], [AV], "veera", 8, 7,
              "arjun low angle, sunlight behind his head, dust in the air, calm eyes", "slow push in, he lifts his eyes",
              trailer=True, punch=True),
        _shot("sc002", 6, [4], "MS", "orbit", [A], [AV], "veera", 9, 6,
              "arjun mid-spin kick, three goons flying back into market stalls", "360 orbit with a speed ramp on impact",
              fps="speed_ramp", action="punch_combo_3hit", trailer=True, sfx=["punch_heavy", "crowd_cheer_te"]),
        _shot("sc002", 7, [4], "WS", "static", [], [], "veera", 8, 4,
              "Goons crashing through bamboo stalls, baskets flying, villagers stepping back", "stalls collapse",
              sfx=["wood_crash"]),
        _shot("sc002", 8, [5], "MCU", "static", [A], [AV], "veera", 8, 7,
              "arjun standing over a goon on the ground, looking down, crowd behind", "he crouches slightly as he speaks",
              punch=True),
    ],
    "sc003": [
        _shot("sc003", 1, [1], "WS", "static", [M, A], [MS, AV], "shringara", 3, 6,
              "River ghat steps at golden hour, meera in a maroon saree floating an oil lamp, arjun sitting on the steps",
              "the lamp drifts out on the water", trailer=True),
        _shot("sc003", 2, [1], "insert", "static", [], [], "shringara", 3, 3,
              "A small clay oil lamp floating on golden river water", "the flame flickers as it drifts"),
        _shot("sc003", 3, [2], "CU", "push_in", [M], [MS], "shringara", 4, 7,
              "meera close-up, warm backlight, jasmine in her braid, worried smile", "gentle push in"),
        _shot("sc003", 4, [3], "CU", "static", [A], [AV], "shringara", 4, 5,
              "arjun close-up, soft smile, golden light on his face", "he glances at her"),
        _shot("sc003", 5, [4], "two_shot", "static", [M, A], [MS, AV], "hasya", 5, 5,
              "meera splashing river water at arjun on the ghat steps, both laughing", "water splashes, he flinches and laughs",
              trailer=True, sfx=["water_splash"]),
        _shot("sc003", 6, [4], "EWS", "static", [M, A], [MS, AV], "shringara", 3, 4,
              "Silhouettes of two people on the ghat steps against a huge orange sunset over the river",
              "birds cross the sky"),
    ],
    "sc004": [
        _shot("sc004", 1, [1], "WS", "pan", [], [], "veera", 5, 5,
              "Noon on the riverbank, fishermen loading baskets of fish into wooden boats", "slow pan along the boats"),
        _shot("sc004", 2, [1], "MS", "track", [A], [AV], "veera", 6, 5,
              "arjun pushing a wooden boat into the river with both hands, sleeves rolled", "the boat slides into the water"),
        _shot("sc004", 3, [2], "MCU", "push_in", [A], [AV], "veera", 7, 8,
              "arjun standing in knee-deep water speaking to the fishermen, noon sun", "push in as his voice rises",
              punch=True),
        _shot("sc004", 4, [3], "WS", "crane_up", [], [], "veera", 7, 5,
              "Fishermen cheering and raising oars above their heads on the bank", "crane up over the raised oars",
              trailer=True, sfx=["crowd_cheer_te"]),
        _shot("sc004", 5, [3], "EWS", "drone", [], [], "veera", 6, 7,
              "A line of wooden fishing boats sailing out together on the wide river", "drone pulls back and rises"),
    ],
    "sc005": [
        _shot("sc005", 1, [1], "WS", "static", [B], [BW], "raudra", 6, 6,
              "Old mansion hall at night, oil lamps, a bruised henchman kneeling before bhairava on a carved chair",
              "lamp flames flicker"),
        _shot("sc005", 2, [1], "insert", "static", [], [], "raudra", 6, 4,
              "A thick gold ring being turned slowly on a man's finger, lamplight", "the ring turns"),
        _shot("sc005", 3, [2], "CU", "push_in", [B], [BW], "raudra", 7, 8,
              "bhairava close-up in lamplight, jaw tight, eyes narrowing", "slow push in"),
        _shot("sc005", 4, [3], "insert", "static", [], [], "raudra", 8, 4,
              "A glass cracking in a man's fist, shards falling to a stone floor in slow motion", "glass breaks",
              fps="slowmo", trailer=True, sfx=["glass_break"]),
        _shot("sc005", 5, [3], "MCU", "static", [B], [BW], "raudra", 8, 8,
              "bhairava staring straight into the camera, lamplight from below", "he does not blink", trailer=True),
    ],
    "sc006": [
        _shot("sc006", 1, [1], "insert", "push_in", [], [], "raudra", 8, 5,
              "An old faded photograph on a wooden table: a proud fisherman in his forties standing by his boat, "
              "a small scar above his left eyebrow", "slow push into the photo",
              sfx=["paper_slide"]),
        _shot("sc006", 2, [2], "ECU", "crash_zoom", [B], [BW], "raudra", 9, 6,
              "bhairava's eyes widening in lamplight", "crash zoom as rage builds"),
        _shot("sc006", 3, [2], "CU", "static", [B], [BW], "raudra", 9, 8,
              "bhairava close-up, rage, sweat, lamplight", "he speaks through his teeth", trailer=True, punch=True),
        _shot("sc006", 4, [3], "EWS", "static", [], [], "raudra", 9, 6,
              "An old mansion on a hill at night lit by a lightning flash", "lightning flashes twice",
              sfx=["thunder"]),
    ],
    "sc007": [
        _shot("sc007", 1, [1], "EWS", "static", [], [], "karuna", 6, 6,
              "Rainy night on a riverbank twenty years ago, torches, silhouettes of men", "rain falls, torches sway",
              sfx=["rain_heavy"]),
        _shot("sc007", 2, [1], "MS", "handheld", [], [], "karuna", 7, 6,
              "Men dragging a fisherman away in the rain, seen from behind", "they pull him into the dark"),
        _shot("sc007", 3, [2], "insert", "static", [], [], "karuna", 7, 6,
              "A small hand-carved wooden toy boat drifting away on dark rainy river water, torchlight reflections",
              "the toy boat drifts out of frame"),
        _shot("sc007", 4, [2], "insert", "static", [], [], "karuna", 7, 4,
              "A broken wooden oar lying in the mud in heavy rain", "rain hammers the mud"),
        _shot("sc007", 5, [3], "WS", "static", [], [], "karuna", 7, 8,
              "Jeep headlights cutting through rain on an empty riverbank at night", "the headlights drive away"),
    ],
    "sc008": [
        _shot("sc008", 1, [1], "EWS", "static", [], [], "karuna", 6, 6,
              "Burnt wooden fishing boats smouldering on a riverbank at dusk, smoke rising", "smoke drifts", trailer=True),
        _shot("sc008", 2, [1], "WS", "static", [A], [AV], "karuna", 6, 5,
              "arjun sitting alone in the ash among burnt boats, holding a burnt oar", "embers drift past"),
        _shot("sc008", 3, [2], "two_shot", "push_in", [M, A], [MS, AV], "karuna", 7, 7,
              "meera kneeling beside arjun, her hand on his shoulder, dusk light", "very slow push in"),
        _shot("sc008", 4, [3], "CU", "push_in", [A], [AV], "veera", 8, 8,
              "arjun close-up, ash on his face, eyes turning hard", "slow push in", trailer=True, punch=True),
        _shot("sc008", 5, [4], "insert", "static", [], [], "veera", 8, 4,
              "A man's hand tightening around a burnt oar", "knuckles whiten"),
    ],
    "sc009": [
        _shot("sc009", 1, [1], "EWS", "crane_up", [A, B], [AC, BW], "veera", 9, 6,
              "Torch-lit riverbank at night, henchmen in a line on one side, villagers with torches behind arjun",
              "crane up to reveal the crowd", trailer=True),
        _shot("sc009", 2, [1], "MS", "track", [A], [AC], "veera", 9, 5,
              "arjun walking forward in torchlight, villagers stepping in behind him", "tracking backwards in front of him",
              fps="slowmo"),
        _shot("sc009", 3, [2], "MCU", "static", [B], [BW], "raudra", 8, 6,
              "bhairava smiling in torchlight, henchmen behind him", "he spreads his hands"),
        _shot("sc009", 4, [3], "MCU", "push_in", [A], [AC], "veera", 10, 7,
              "arjun in torchlight, torn shirt, steady eyes", "push in", trailer=True, punch=True),
        _shot("sc009", 5, [4], "MS", "orbit", [A], [AC], "veera", 10, 6,
              "arjun fighting through henchmen in torchlight, sparks and dust", "orbit with a speed ramp on each hit",
              fps="speed_ramp", action="punch_combo_3hit", sfx=["punch_heavy", "body_fall"]),
        _shot("sc009", 6, [4], "MS", "handheld", [A, B], [AC, BW], "veera", 10, 5,
              "arjun and bhairava grappling at the water's edge", "they struggle, torches swing behind",
              sfx=["punch_heavy"]),
        _shot("sc009", 7, [4], "WS", "static", [A, B], [AC, BW], "veera", 10, 6,
              "arjun throwing bhairava into the shallow river, a huge splash in torchlight", "slow-motion splash",
              fps="slowmo", sfx=["water_splash"]),
        _shot("sc009", 8, [4], "MCU", "static", [A], [AC], "veera", 9, 4,
              "arjun standing in torchlight breathing hard, villagers cheering behind", "the crowd raises torches",
              sfx=["crowd_cheer_te"]),
    ],
    "sc010": [
        _shot("sc010", 1, [1], "EWS", "drone", [], [], "shanta", 4, 6,
              "Dawn over the Godavari, wooden boats being pushed back into the water", "drone rises slowly"),
        _shot("sc010", 2, [1], "two_shot", "static", [M, A], [MS, AV], "shanta", 4, 5,
              "meera and arjun standing on the bank at dawn, smiling", "a breeze moves her saree"),
        _shot("sc010", 3, [2], "WS", "static", [], [], "shanta", 4, 4,
              "Fishermen carrying nets along the riverbank at dawn, laughing", "they walk past the camera"),
    ],
}

CUE_REVISIONS = {"revisions": [
    {"cue_id": "cue007", "raga": "shivaranjani", "tala": None, "bpm": None, "instruments": None,
     "leitmotif_character": "arjun", "silence_before_s": None,
     "reason": "Flashback and burnt boats: the hero theme in Shivaranjani, his wound."},
    {"cue_id": "cue001", "raga": None, "tala": None, "bpm": None,
     "instruments": ["low_drones", "dissonant_strings", "breath_fx"], "leitmotif_character": None,
     "silence_before_s": None, "reason": "Open on Bhairava's dread, no melody."},
]}


def _with_context(scene: dict, sid: str) -> dict:
    meta = next(s for s in OUTLINE["scenes"] if s["scene_id"] == sid)
    return {"scene_id": sid, "heading": meta["heading"], "synopsis": meta["synopsis"],
            "rasa_primary": meta["rasa_primary"], "intensity": meta["intensity"], "blocks": scene}


def create_poc(root: Path, automatic: bool = True) -> Path:
    """Write the POC project. Refuses to overwrite an existing story bible.

    automatic=True uses the Veo API and Gemini TTS; False sets up the manual shot-pack flow.
    """
    if (root / "01_story_bible.json").exists():
        raise FileExistsError(f"{root} already has a story bible")
    (root / "input").mkdir(parents=True, exist_ok=True)
    (root / "input" / "story.md").write_text(POC_PLOT, encoding="utf-8")
    config = POC_CONFIG if automatic else deep_merge(POC_CONFIG, MANUAL_OVERRIDES)
    (root / "pipeline.yaml").write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    (root / "pricing.yaml").write_text(yaml.safe_dump(POC_PRICING, sort_keys=False), encoding="utf-8")

    bible = StoryBible.model_validate(STORY_BIBLE)
    outline = Outline.model_validate(OUTLINE)
    (root / "01_story_bible.json").write_text(bible.model_dump_json(indent=2), encoding="utf-8")
    (root / "02_outline.json").write_text(outline.model_dump_json(indent=2), encoding="utf-8")
    for sid, blocks in SCENES.items():
        script = SceneScript.model_validate(_with_context(blocks, sid))
        shots = ShotList.model_validate({"scene_id": sid, "shots": SHOTS[sid]})
        for folder, model in (("03_scenes", script), ("04_shots", shots)):
            (root / folder).mkdir(exist_ok=True)
            (root / folder / f"{sid}.json").write_text(model.model_dump_json(indent=2), encoding="utf-8")

    # Library-first casting needs no LLM when every character matches; the lock lets the
    # cue planner attach only leitmotifs that exist. The pipeline's cast stage writes the real lock.
    cast, _ = CastingAgent(None, {}).cast(bible, AssetRegistry.load(REPO_ROOT / "library"))
    navarasa = load_navarasa()
    sheet, _ = apply_revisions(plan_cues(outline, bible, navarasa, cast),
                               CueRevisions.model_validate(CUE_REVISIONS).revisions, navarasa)
    (root / "08_music").mkdir(exist_ok=True)
    (root / "08_music" / "cue_sheet.json").write_text(sheet.model_dump_json(indent=2), encoding="utf-8")
    (root / "reports").mkdir(exist_ok=True)
    (root / "reports" / "authored_in_claude_code.json").write_text(
        json.dumps({"stages": ["story_bible", "outline", "scenes", "shots", "music_plan"]}), encoding="utf-8")
    return root
