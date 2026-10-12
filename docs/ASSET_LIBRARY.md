# The Asset Library: Characters, Voices, Music, Actions, Locations, SFX

The library is the repository the pipeline searches **before** it generates anything new.
It is what lets you:

- keep the **same characters** across a film (and across a franchise or series),
- give every film the **same Telugu sound**: your stems, motifs and voice artists,
- reuse **choreography, camera moves and locations** instead of paying to regenerate them,
- keep **licence and consent** records next to every asset.

Code: `src/nraiorbit/library/registry.py`. Manifest schemas: `src/nraiorbit/schemas.py`
(`*Asset` classes).

---

## 1. Layout

```
library/
  characters/<id>/
    manifest.yaml               # kind: character
    refs/face/*.png             # 12–20 angle + expression refs
    refs/body/*.png             # A-pose front/side/back + action poses
    refs/costumes/<costume_id>/*.png
    lora/<name>.safetensors     # identity LoRA(s), one per base model
    embeddings/face_centroid.npy
    embeddings/body_centroid.npy
  voices/<id>/
    manifest.yaml               # kind: voice
    samples/clean/*.wav
    samples/emotion/<rasa>/*.wav  (+ .txt transcripts)
    samples/efforts/*.wav
    models/                     # fine-tuned TTS / voice-conversion checkpoints
    embeddings/speaker_centroid.npy
  voices/lexicon_te.yaml        # pronunciation lexicon
  music/
    stems/<id>/manifest.yaml + audio (wav, 48 kHz, per stem)
    motifs/<id>/manifest.yaml + motif.mid + rendered stems
  actions/<id>/
    manifest.yaml               # kind: action (fight, chase, dance, stunt, walk…)
    mocap.bvh | mocap.fbx       # motion capture
    pose.mp4                    # rendered skeleton video for pose-conditioned generation
    reference.mp4               # optional live-action reference
  locations/<id>/
    manifest.yaml + refs/*.png + depth/*.png + ambience.wav
  sfx/<id>/
    manifest.yaml + audio
```

**Manifests go in git. Binaries go in object storage** (S3/R2/GCS/MinIO) via
Git LFS or DVC. A manifest references files by relative path, and `nraiorbit library
validate` reports missing files, missing licences and missing consent records.

---

## 2. Manifest essentials

Every manifest has:

```yaml
id: arjun_hero
kind: character            # character | voice | music_stem | motif | action | location | sfx
version: 3
name: Arjun
tags: [hero, male, 30s, rugged, rayalaseema, village]
license:
  type: owned              # owned | commissioned | licensed | cc0 | cc_by
  commercial_use: true     # must be true for anything used in a monetised film
  source: "in-house generation, 2026-09"
  document_ref: null
consent:                   # required for faces/voices derived from real people
  required: false
  document_ref: null
```

Kind-specific fields (see the example manifests in `library/`):

| Kind | Key fields |
|---|---|
| `character` | `physical` (gender, age, height_cm, build, skin_tone_lab, hair, eyes, marks), `costumes[]`, `refs`, `lora`, `embeddings`, `guard` (threshold overrides), `default_voice` |
| `voice` | `dialect`, `gender`, `age_range`, `pitch`, `timbre_tags`, `emotion_refs{rasa: [{path, transcript_te, intensity, pace}]}`, `models`, `embeddings` |
| `music_stem` | `role` (groove/melody/texture/hit/riser/chant/drone), `raga`, `tala`, `bpm`, `sa_note`, `rasas[]`, `intensity[min,max]`, `instruments[]`, `duration_s`, `loopable` |
| `motif` | `for_role`, `raga`, `sa_note`, `bpm`, `midi`, `stems` |
| `action` | `category`, `rasas[]`, `intensity[min,max]`, `performers`, `duration_s`, `mocap`, `pose_video`, `camera_suggestions[]`, `sfx_cues[]`, `description` |
| `location` | `region`, `setting_type`, `times_of_day[]`, `refs`, `ambience`, `description` |
| `sfx` | `category`, `duration_s`, `file` |

---

## 3. How the code fetches assets automatically

```python
from nraiorbit.library.registry import AssetRegistry

lib = AssetRegistry.load("library")
# Director needs a fight template for a high-intensity veera beat with 1 performer vs many:
hits = lib.search("action", tags=["fight", "hero_vs_group"], rasa="veera", intensity=8, k=3)
# Music planner wants a dappu groove near 132 BPM for a mass cue:
grooves = lib.search("music_stem", tags=["groove", "dappu"], rasa="veera", bpm=132, k=5)
```

Search scoring (tag-based in the scaffold, embedding-based in production):

```
score = 0.5 * tag_overlap(query.tags, asset.tags)
      + 0.3 * rasa_match(query.rasa, asset.rasas)
      + 0.2 * range_fit(query.intensity / bpm, asset ranges)
```

Hard filters (kind, `license.commercial_use`, consent present) apply before scoring, so
an unlicensed asset is never returned for a production render.

In production, add text and image embeddings (SigLIP for images, CLAP for audio, a
text embedder for descriptions) to pgvector or Qdrant, and blend vector similarity into
the score. The Python API stays the same.

### Casting and the lockfile

The Casting agent calls `search("character", …)` with the Story Bible's character spec.
Above the match threshold, it proposes the existing asset. Below it, it writes a brief for
a new identity pack. After the **cast** gate, `cast.lock.yaml` pins `asset@version` for
every character, voice and motif. From then on, every stage resolves characters **only**
through the lockfile (see [ARCHITECTURE.md §4](ARCHITECTURE.md#4-the-project-lockfile-is-the-guard-trail)).

---

## 4. Building the library: first 90 days

| Asset | Minimum to start | Good | How to get it |
|---|---|---|---|
| Characters | 4 leads (hero, heroine, villain, one supporting) | 10–15 recurring characters | Generate + curate identity packs; train LoRAs |
| Voices | 4 lead voices, ≥ 20 min each, all 11 rasas | 10–15 voices + 6 crowd voices per dialect | Telugu dubbing artists under AI-voice consent contracts |
| Music stems | 150 stems: grooves for Adi/Rupaka/Misra Chapu/teenmaar at 3–4 tempos, phrases in 10 core ragas, hits/risers | 1,000+ stems | Commission Telugu/Carnatic/folk musicians (multitrack sessions); licensed sample packs |
| Motifs | 1 per lead | 1 per recurring character + 2 film themes | Composer (human or symbolic engine) |
| Actions | 30 templates: walks, hero entries, 10 fight combos, chases, dance steps | 200+ | Mocap (suit or markerless phone capture), stock mocap libraries |
| Locations | 10 (village, temple, riverbank, market, city street, home interior, police station, highway, palace, fields) | 50+ | Generated plates + depth maps; your own photos and video |
| SFX | Licensed general pack + Telugu crowd walla + festival ambience | Custom Foley library | Licensed packs; field recordings in Andhra Pradesh / Telangana |

The example manifests in `library/` show the format for each kind. They reference media
files that are not committed; `nraiorbit library validate` lists them as missing until
you add them.
