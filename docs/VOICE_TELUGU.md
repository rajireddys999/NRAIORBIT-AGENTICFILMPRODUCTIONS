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

### 5.1 Speaker lock (`lipsync.speaker_lock`, on by default)

LatentSync redraws the mouth of the **largest** face it finds in each frame. A frame-by-frame review of the
10-minute film found it animating the wrong person in 9 of 34 shots: an out-of-focus foreground head, an
over-the-shoulder ear, a henchman nearer the lens. The real speaker's mouth then flipped between synced and unsynced,
and stray mouths, smears and blobs appeared on other people and backgrounds. `post/facelock.py` fixes this without
changing the GPU worker:

1. **Find the speaker.** MediaPipe BlazeFace (Apache-2.0) finds every face in every frame and links them into
   tracks. The video model is told that only the speaker talks, so the speaker is the in-focus face whose mouth moves
   the most. A point in the config overrides this: `lipsync.shots.<shot_id>.speaker: [x, y]`. A review sheet is
   saved next to each clip (`<take>.speaker.jpg`, speaker boxed in green).
2. **Show LatentSync only the speaker.** It gets a crop around the speaker, with every other face filled flat.
   Frames where the speaker's face is not visible are left as filmed.
3. **Change only the speaker's mouth.** The synced pixels go back inside a feathered ellipse over the mouth and jaw.
   The speaker's own eyes stay as filmed (LatentSync softens the whole face it redraws). The original's skin,
   stubble and moustache texture goes back onto the synced skin wherever its shape is unchanged. It never goes onto
   the lips, where the old teeth would ghost (`lipsync.detail`).
   With `lipsync.mesh` (on by default) the ellipse is placed on the speaker's **face mesh** (MediaPipe Face
   Landmarker, 478 points, Apache-2.0) and clipped to the face's outline. On a three-quarter face BlazeFace's six
   keypoints slide onto the cheek, and the plain ellipse covered the cheek while the real mouth stayed unsynced.
   The plain ellipse also reached past a turned face onto the background, where LatentSync's smears showed as pale
   blobs on water, foliage, collars and blouses. The mesh keeps the patch on the real lips and off the background,
   with room below the jaw for an opening mouth.
4. **Reuse paid work safely.** A whole-frame result made earlier is reused through the same mask only if it edited
   the speaker. In each frame the change on the speaker's mouth is compared with the strongest change anywhere
   away from the speaker's face (`lock_ratio`, `lock_floor`). Calibrated on the film: it found 42 of 43, 92 of 92
   and 5 of 5 known wrong-face frames, with no false alarms on the correct shots.

Per-shot fixes from review go in `lipsync.shots.<shot_id>`:

| Setting | Effect |
|---|---|
| `{sync: false}` | Keep the shot exactly as filmed |
| `{keep_frames: [[a, b]]}` | Frames a to b stay as filmed and LatentSync is not given them, e.g. a hand over the mouth or a turn to profile. In crop mode this makes new crops, so a new GPU job |
| `{filmed_frames: [[a, b]]}` | Frames a to b are shown as filmed in the paste-back only. The paid result is reused, so this is free. Use it for a smear, melt or flash found in review |
| `{fade_frames: 2}` | The synced mouth dissolves in and out over 2 frames next to frames kept as filmed, instead of a hard cut between two mouths |
| `{repair_frames: [[a, b]]}` | Frames between a and b are rebuilt from those two, e.g. a 4-frame mouth smear |
| `{protect: {above: 190}}` | Original pixels brighter than this stay as filmed (`below` for darker), e.g. backlit water beside a cheek |
| `{bridge_gaps: 8}` | Close gaps of up to 8 frames where the detector lost the face mid-line. In crop mode the gap takes the whole-frame result |
| `{extend_track: true}` | Another track of the same face carries the speaker's on, when the detector split one face in two. A number sets the least box overlap that counts (default 0.3) |
| `{sync_s: 4.8}` | The shot was cut shorter in the edit after lip-sync. Its take stays at the synced length, so the paid lip-sync is reused, and the edit plays only the start. Use it to cut a shot right after its line when the filmed mouth goes on talking in silence |
| `{cut_in_s: 0.25}` | The edit cuts into the shot this much later. In `04_shots` shorten the shot and lengthen the one before it by the same amount. The take, its lip-sync and its lines keep their film times, so the paid lip-sync is reused, and the assembly starts the clip that many frames in. Use it when the filmed mouth shouts in silence before the line starts |
| `{mesh: false}`, `{detail: 0.5}` | Per-shot overrides of the global settings |
| `{mode: crop}` | Never reuse a whole-frame result |

`lipsync.offline: true` rebuilds from results already made and never starts a GPU job. Install the face models
with `pip install -e '.[lipsync]'`. They are downloaded once from Google's model storage: the detector is 1 MB and
the face mesh 4 MB.

The assembly puts every cut on the film's frame grid. Each clip is a whole number of frames, while the dialogue sits at
exact times. Rounding each clip on its own used to add up: by the last reel the voice was 92 ms behind the lips, and
125 ms by the end. Now a shot fills frames round(start x fps) up to round(end x fps), trimming or holding its last
frame, so picture and sound are never more than half a frame apart.
