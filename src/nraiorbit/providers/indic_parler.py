"""Telugu TTS with AI4Bharat Indic Parler-TTS, runs on a Mac (Apple GPU via MPS), NVIDIA, or CPU.

    pip install torch soundfile "git+https://github.com/huggingface/parler-tts.git"

The voice is steered by a text description built from the voice manifest (gender, pitch,
timbre, optional named speaker) plus the line's delivery (emotion, pace, volume). A named
speaker from the model card keeps a character's voice most consistent across lines.
Written from the model card; not run in CI (the model is a ~1B-parameter download).
Check the model licence before commercial use.
"""

from __future__ import annotations

from pathlib import Path

from ..schemas import ScriptBlock
from .base import register

MODEL_ID = "ai4bharat/indic-parler-tts"
PACE = {"very_slow": "very slowly", "slow": "slowly", "medium": "at a moderate pace", "fast": "quickly",
        "very_fast": "very quickly"}
VOLUME = {"whisper": "in a whisper", "low": "softly", "normal": "", "loud": "loudly", "shout": "shouting"}
EMOTION = {
    "shringara": "warm and affectionate", "hasya": "playful and amused", "karuna": "sad and emotional",
    "raudra": "angry and intense", "veera": "confident and commanding", "bhayanaka": "fearful and tense",
    "bibhatsa": "disgusted", "adbhuta": "amazed", "shanta": "calm and peaceful", "vatsalya": "tender and loving",
    "bhakti": "devout and reverent",
}


def describe(voice: dict, line: ScriptBlock) -> str:
    who = voice.get("tts_speaker") or f"A {voice.get('gender', 'male')} speaker"
    pitch = {"low": "a deep, low-pitched", "mid": "a medium-pitched", "high": "a high-pitched"}.get(
        voice.get("pitch") or "mid", "a medium-pitched")
    timbre = ", ".join(voice.get("timbre_tags", [])[:2])
    d = line.delivery
    emotion = EMOTION.get(d.rasa, "expressive") if d else "expressive"
    pace = PACE.get(d.pace, "at a moderate pace") if d else "at a moderate pace"
    volume = VOLUME.get(d.volume, "") if d else ""
    return (f"{who} with {pitch}{', ' + timbre if timbre else ''} voice speaks {pace} {volume} "
            f"in a {emotion} tone. The recording is very clear and close-up, with no background noise.").replace("  ", " ")


@register("tts", "indic_parler")
class IndicParlerTTS:
    def __init__(self, voices: dict[str, dict] | None = None, device: str | None = None):
        import torch
        from parler_tts import ParlerTTSForConditionalGeneration
        from transformers import AutoTokenizer

        if device is None:
            device = "cuda" if torch.cuda.is_available() else "mps" if torch.backends.mps.is_available() else "cpu"
        self.device = device
        self.model = ParlerTTSForConditionalGeneration.from_pretrained(MODEL_ID).to(device)
        self.tokenizer = AutoTokenizer.from_pretrained(MODEL_ID)
        self.description_tokenizer = AutoTokenizer.from_pretrained(self.model.config.text_encoder._name_or_path)
        self.voices = voices or {}

    def synthesize(self, line: ScriptBlock, voice_ref: str | None, out: Path) -> float:
        import soundfile as sf

        description = describe(self.voices.get(voice_ref or "", {}), line)
        desc = self.description_tokenizer(description, return_tensors="pt").to(self.device)
        prompt = self.tokenizer(line.text_te, return_tensors="pt").to(self.device)
        audio = self.model.generate(
            input_ids=desc.input_ids, attention_mask=desc.attention_mask,
            prompt_input_ids=prompt.input_ids, prompt_attention_mask=prompt.attention_mask,
        ).cpu().numpy().squeeze()
        out.parent.mkdir(parents=True, exist_ok=True)
        raw = out.with_suffix(".raw.wav")
        sf.write(str(raw), audio, self.model.config.sampling_rate)
        from ..post import ffmpeg

        ffmpeg.run(["-i", str(raw), "-ar", str(ffmpeg.SAMPLE_RATE), "-ac", "2", str(out)])
        raw.unlink()
        return ffmpeg.duration(out)
