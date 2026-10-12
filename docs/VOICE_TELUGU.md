# Telugu Voice: Dialogue That Sounds Like Real Telugu People

Three things make dialogue sound native, and the pipeline handles each separately:

1. **The words:** spoken Telugu (*vyavaharika*) with the character's dialect and a
   realistic amount of English mixed in. Not textbook Telugu (*grandhika*).
2. **The voice:** each character has one consistent voice identity for the whole film.
3. **The performance:** emotion, pace, pauses, breath, efforts (shouts, grunts, crying)
   that match the scene.

---

## 1. The words (Screenwriter + Dialogue Critic)

Each character spec in the Story Bible carries:

```yaml
dialect: rayalaseema          # telangana | coastal_andhra | godavari | rayalaseema | uttarandhra | standard
speech_register: colloquial    # colloquial | formal | rustic | urban_slang
code_mix: light                # none | light | heavy  (English words in Telugu speech)
speech_traits: ["short punchy lines", "calls everyone 'anna'", "pauses before threats"]
catchphrases: ["..."]
```

The Screenwriter writes dialogue **in Telugu script** (so TTS reads it correctly), with an
English subtitle line and delivery tags:

```json
{
  "line_id": "sc014_l007",
  "character_id": "arjun",
  "text_te": "…",
  "text_en": "Touch my family again, and you won't see tomorrow.",
  "delivery": {"rasa": "raudra", "intensity": 8, "pace": "slow", "volume": "low", "notes": "quiet threat, not shouting"}
}
```

The **Dialogue Critic** then checks each scene for:

- bookish or translated-from-English phrasing (rewrite it the way people actually talk),
- dialect consistency per character (vocabulary, verb endings, address terms),
- code-mix level matching the character (a village elder vs. a Hyderabad techie),
- punch-dialogue quality for mass moments (short, rhythmic, quotable),
- names and English words that need a pronunciation-lexicon entry.

### Pronunciation lexicon

`library/voices/lexicon_te.yaml` maps names, places and English words to the phonetic
Telugu spelling the TTS should read (for example, an English brand name written as it is
said in Telugu). The TTS stage applies it before synthesis.

---

## 2. The voice (voice bank + identity)

Every character's voice is a library asset (`library/voices/<id>/`):

| Item | Contents |
|---|---|
| `samples/clean/` | 20–60 min of clean studio speech from a **consenting** Telugu voice artist in the character's dialect (48 kHz, dry, no music) |
| `samples/emotion/<rasa>/` | 5–10 reference clips per rasa (anger, love, grief, comedy, fear, heroism, calm …), 4–10 s each, with transcripts |
| `samples/efforts/` | Shouts, grunts, laughs, cries, breaths, screams for action and emotional beats |
| `models/` | Fine-tuned TTS checkpoint and/or voice-conversion model for this voice |
| `embeddings/speaker_centroid.npy` | ECAPA speaker-embedding centroid for the voice guard |
| `voice.yaml` | Dialect, pitch range, timbre tags, licence/consent reference |

**Consent is required:** record dubbing artists under a written agreement that covers AI
voice synthesis for this project or channel. Do not clone voices of actors or public
figures.

---

## 3. The performance (synthesis paths)

The pipeline supports three paths per line, chosen in `config/pipeline.yaml`
(`voice.mode`). Quality goes down and automation goes up:

| Path | How | Quality | Automation |
|---|---|---|---|
| **A. Performance + conversion** | A Telugu dubbing artist records a scratch read of each line (any voice, real emotion), then voice conversion (RVC / Seed-VC) maps it to the character's voice | Highest: real Telugu prosody and emotion | Needs a recording session |
| **B. Fine-tuned TTS** | A TTS fine-tuned per character voice (e.g. IndicF5 / F5-TTS-style or Indic Parler-TTS) with **emotion-reference retrieval**: pick the reference clip from `samples/emotion/<rasa>/` closest to the target delivery and use it as the prompt | Good, improving fast | Fully automatic |
| **C. API TTS + conversion** | Commercial Telugu TTS (e.g. Sarvam Bulbul, ElevenLabs, Azure/Google te-IN voices), then voice conversion to the character timbre | Good for minor characters | Fully automatic |

A sensible setup is **A for the leads' emotional peaks and punch dialogues, B for
everything else, C for crowd and background voices.**

### Emotion-reference retrieval

Zero-shot or fine-tuned cloning TTS copies the **prosody of its reference clip**, so the
choice of reference *is* the emotion control:

```
target delivery (rasa=karuna, intensity=7, pace=slow)
   → candidate refs = voice.samples.emotion["karuna"]
   → rank by |ref.intensity - 7| + pace match + speech-emotion-model similarity
   → synthesize with the top ref; generate 3 takes; voice guard picks the best
```

### Post-processing per line

- loudness-normalise dialogue to a consistent level before the mix,
- room tone and reverb matched to the location (temple, open field, small room),
- breaths and efforts inserted from `samples/efforts/` at the Director's markers,
- phase-aligned timing to the shot (the Editor allows ±150 ms to fit lip-sync).

---

## 4. Voice guard

Implemented in `src/nraiorbit/guards/voice.py`:

| Check | Tool | Default threshold |
|---|---|---|
| Speaker similarity to character centroid | ECAPA-TDNN embeddings | cosine ≥ 0.70 |
| Script accuracy | Telugu ASR (IndicConformer / Whisper large-v3) → character error rate vs `text_te` | CER ≤ 0.15 |
| Emotion match | Speech emotion recogniser vs target rasa | top-2 contains target |
| Naturalness | UTMOS / DNSMOS predictor | MOS ≥ 3.5 |
| Duration fit | vs shot duration budget | within ±20 % |
| Clipping / noise | peak and SNR checks | no clipping, SNR ≥ 30 dB |

The best of N takes is the one with the highest combined score that passes every check.
A line that fails all takes goes to the review queue, where you can record path A for it.

---

## 5. Lip-sync

After the dialogue WAV is final, dialogue shots go through an audio-driven lip-sync model
(LatentSync, MuseTalk, InfiniteTalk / Wan speech-to-video class models, or a commercial API).
These models work at the phoneme/audio level, so Telugu works without language-specific
training. The identity guard runs again afterwards.
