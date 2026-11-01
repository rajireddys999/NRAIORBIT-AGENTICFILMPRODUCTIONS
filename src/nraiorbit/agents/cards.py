"""The card writer: the owner's words for the title and end cards, turned into a deck the renderer can draw."""

from __future__ import annotations

from ..schemas import CardDeck, StoryBible
from .base import Agent, project_context


class CardWriter(Agent):
    name = "card_writer"

    @property
    def references(self) -> str:
        return ""                                      # cards follow the owner's words, not the studio's references

    def write(self, bible: StoryBible, brief: str) -> CardDeck:
        cast = "\n".join(f"- {c.id}: {c.name_te} / {c.name_en}, {c.role}" for c in bible.characters)
        task = (f"The owner wants these cards before and after the film, in their own words:\n"
                f"<owner_words>\n{brief}\n</owner_words>\n"
                f"The film is {bible.title_te} / {bible.title_en}. Its cast, as the story bible names them:\n{cast}\n"
                "Write the deck: the cards before the film first, then the ones after, each a few lines of Telugu "
                "over English, 2 to 10 seconds, in the style the owner asked for. Keep their words and their order.")
        return self.ask(CardDeck, context=project_context(("story_bible", bible)), task=task)
