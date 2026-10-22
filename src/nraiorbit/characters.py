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
