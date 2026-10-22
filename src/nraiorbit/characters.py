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
