"""Facts about a story bible's characters that more than one part of the system needs, with no heavy imports."""

from __future__ import annotations

import re

ANIMAL_WORDS = re.compile(r"\b(fur|furry|macaque|monkey|langur|ape|animal|tail|paws?|snout|muzzle|feathers|beak|hooves|"
                          r"mane|whiskers)\b")


def animal_physical(physical) -> bool:
    """Whether a character's physical description (a PhysicalSpec or its dict) is an animal's: fur, a tail, a
    macaque... Word by word, so a ponytail or a furrowed brow stays a person's."""
    get = physical.get if isinstance(physical, dict) else (lambda k, d="": getattr(physical, k, d))
    text = " ".join(str(get(k, "") or "") for k in ("build", "skin_tone", "face", "hair")).lower()
    return bool(ANIMAL_WORDS.search(text))



def marked_animal(spec) -> bool:
    """The story bible says this character is an animal that never talks (CharacterSpec.silent_animal). Only this,
    never the words of its description, decides that its lines are its sounds: a person written with "a lion's
    build", a god with a tail or a talking animal keeps every line."""
    return bool(spec.get("silent_animal") if isinstance(spec, dict) else getattr(spec, "silent_animal", False))


def talking_animals(cfg: dict):
    """voice.animals_speak as a rule: True (every animal talks) or the set of ids that talk. false, a missing value or
    "no" is the empty set; true, "true", "yes" or "all" is True; one id or a list of ids is that set."""
    talk = (cfg.get("voice") or {}).get("animals_speak")
    if talk is True or (isinstance(talk, str) and talk.strip().lower() in ("true", "yes", "all")):
        return True
    if isinstance(talk, str) and talk.strip().lower() not in ("", "false", "no", "none"):
        return {talk.strip()}
    return {str(t) for t in talk} if isinstance(talk, (list, tuple, set)) else set()


def silent_animals(characters, cfg: dict) -> dict[str, str]:
    """{id: English name} of the characters the story bible marks silent_animal, less any voice.animals_speak
    lets talk."""
    talk = talking_animals(cfg)
    if talk is True:
        return {}
    return {c.id: c.name_en for c in characters if marked_animal(c) and c.id not in talk}


def children_refused(cfg: dict) -> bool:
    """Whether this project's video model refuses to show anyone under 18: Veo animates a child only when
    google.person_generation is allow_all, which Google grants to allowlisted projects."""
    if (cfg.get("providers") or {}).get("video") != "veo":
        return False
    return str((cfg.get("google") or {}).get("person_generation") or "allow_adult").lower() != "allow_all"


# a baby held, carried or fed like a human infant: Google's filter reads the frame as a child, whatever the baby is
INFANT_WORDS = re.compile(r"\b(baby|babies|infant|infants|newborn|newborns)\b")
HELD_WORDS = re.compile(r"\b(cradl\w*|swaddl\w*|in (?:his|her|their) arms|crook of (?:his|her|their) arm|"
                        r"against (?:his|her|their) chest|nestl\w* (?:into|against|in) (?:his|her|their) "
                        r"(?:chest|arms|lap)|spoon-fed|bottle-fed|spoon of (?:warm )?milk|suckl\w*)\b")
ROCKED = re.compile(r"\brock(?:s|ed|ing)? (?:the|a|his|her|their|its) (?:\w+ ){0,3}(?:baby|infant|newborn)\b")
NEAR_WORDS = 10
CHILD_WORDS = re.compile(r"\b(child|children|kids?|schoolboys?|schoolgirls?|schoolchildren|toddlers?|"
                         r"little (?:boy|girl)s?|young (?:boy|girl)s?)\b")
NOT_A_CHILD = re.compile(r"\bkids? at heart\b|\b(?:goat|lamb) kids?\b|\bkid goats?\b|\bchild-?like\b|\bwith child\b|"
                         r"\bchildren'?s day\b|\b(?:young|little) (?:boy|girl)s?\b(?=[^.]{0,40}?\b(?:1[89]|[2-9]\d)\b)")


def _word_at(text: str, i: int) -> int:
    return len(text[:i].split())


def cradled_infant(text: str) -> bool:
    """A shot's words show a baby held like a human infant (a baby monkey cradled in a man's arms, rocked, fed from a
    spoon): the holding within NEAR_WORDS words of the baby, so a baby goat by the rocks stays a goat."""
    text = text.lower()
    if ROCKED.search(text):
        return True
    babies = [_word_at(text, m.start()) for m in INFANT_WORDS.finditer(text)]
    held = [_word_at(text, m.start()) for m in HELD_WORDS.finditer(text)]
    return any(abs(b - h) <= NEAR_WORDS for b in babies for h in held)


def child_words(text: str) -> list[str]:
    """Words that describe a child who is not in the cast (village kids, a schoolgirl); not a goat's kid, a kid at
    heart, Children's Day or a young girl of 25."""
    return sorted({m.group(0) for m in CHILD_WORDS.finditer(NOT_A_CHILD.sub(" ", text.lower()))})


def near_infant(text: str, word: str) -> bool:
    """`word` (a character's id or name) within NEAR_WORDS words of a baby word: the baby is that character."""
    text = text.lower()
    babies = [_word_at(text, m.start()) for m in INFANT_WORDS.finditer(text)]
    at = [_word_at(text, m.start()) for m in re.finditer(rf"\b{re.escape(word.lower())}\b", text)]
    return any(abs(b - w) <= NEAR_WORDS for b in babies for w in at)


def minors_off_screen(characters, cfg: dict) -> dict[str, str]:
    """{id: "Name (age)"} of the people under 18 that this project's video model must not show (children_refused).
    Animals are never counted (their ages are animal ages). Empty for other video providers or with allow_all."""
    if not children_refused(cfg):
        return {}
    out = {}
    for c in characters:
        physical = c.physical
        age = physical.get("age") if isinstance(physical, dict) else getattr(physical, "age", None)
        if isinstance(age, (int, float)) and age < 18 and not marked_animal(c) and not animal_physical(physical):
            out[c.id] = f"{c.name_en} ({int(age)})"
    return out
