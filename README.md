# NRAIORBIT Agentic Film Productions

An agentic pipeline that turns a **story plot prompt** into a **Telugu feature film**
(1h45m), a **trailer** and subtitles, ready for a **YouTube** release:

- **Story → screenplay:** Claude agents analyse the plot, build a Story Bible, outline
  the film on a Telugu commercial structure (hero intro, interval bang, climax), and write
  every scene in **spoken Telugu** with per-character dialects.
- **Same face and body throughout:** characters are cast from an asset library, pinned in
  a lockfile, and every generated shot passes an **identity guard** (face + body
  embeddings, drift and swap detection) or gets retaken.
- **Native Telugu voice:** per-character voice bank from consenting Telugu artists,
  emotion-matched reference clips, and a voice guard (speaker, script accuracy, emotion,
  quality).
- **Native Telugu score:** a navarasa → raga/tala/instrument mapping drives the cue
  plan (mass elevation, sentiment, romance, comedy …), with leitmotifs, recorded
  Telugu/Carnatic/folk stems and a raga-conformance check.
- **Edit, trailer, publish:** frame-accurate assembly, dialogue-ducked music,
  −14 LUFS master, Telugu + English subtitles, an automatic trailer cutter, and YouTube
  upload with the synthetic-media disclosure.

## Design documents

| Doc | Contents |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | System overview, stages S0–S16, lockfile, retake loop, storage layout |
| [docs/AGENTS.md](docs/AGENTS.md) | The decision-making agents, specialist models, critique loops, feedback flywheel |
| [docs/CHARACTER_IDENTITY.md](docs/CHARACTER_IDENTITY.md) | Identity packs, conditioning, the identity guard and its thresholds |
| [docs/VOICE_TELUGU.md](docs/VOICE_TELUGU.md) | Dialects, voice bank, three synthesis paths, voice guard, lip-sync |
| [docs/MUSIC_TELUGU.md](docs/MUSIC_TELUGU.md) | Navarasa mapping, cue planning, mass elevation recipe, composer engine, music guard |
| [docs/ASSET_LIBRARY.md](docs/ASSET_LIBRARY.md) | The asset repository: layout, manifests, retrieval, building it in 90 days |
| [docs/TOOLS.md](docs/TOOLS.md) | Every tool and model per capability, with licence warnings and a starter stack |
| [docs/INFRA_COST_ROADMAP.md](docs/INFRA_COST_ROADMAP.md) | Deployment, GPU hours and cost estimates, compliance checklist, phased roadmap |
| [docs/POC_RUNBOOK.md](docs/POC_RUNBOOK.md) | **Start here:** the 5-minute POC on a Mac + Veo + DaVinci Resolve, with its bill |

## 5-minute POC and its bill

```bash
nraiorbit poc godavari5                 # screenplay + 54 shots + cue sheet, already written
nraiorbit estimate godavari5            # expected ≈ $34 (Veo Lite) / $66 (Veo Fast) / $258 (Veo) in total
nraiorbit shotpack export godavari5     # Veo prompts, reference images, recording script, cue sheet
# generate clips in Veo -> projects/godavari5/inbox/video/<shot_id>.mp4
nraiorbit run godavari5 --force render  # 1080p movie, stems for Resolve, te/en subtitles
nraiorbit estimate godavari5            # now with "Actual so far"
```

Full steps in [docs/POC_RUNBOOK.md](docs/POC_RUNBOOK.md). `nraiorbit estimate --runtime 105`
estimates a full feature before any plan exists.

## Quick start

Requires Python 3.11+ and ffmpeg.

```bash
pip install -e ".[dev]"

# 1. Offline demo: recorded agent outputs + placeholder (animatic) render. No API key, no GPU.
nraiorbit demo godavari
#    -> projects/godavari/11_master/{movie.mp4, trailer.mp4, subs_te.srt, subs_en.srt}

# 2. Real run with Claude (set ANTHROPIC_API_KEY, or log in with `ant auth login`)
nraiorbit new my-film --story examples/story_godavari.md
nraiorbit run my-film            # pauses at review gates: story, cast, pacing, trailer
nraiorbit approve my-film story
nraiorbit run my-film            # resumes where it stopped; finished stages are cached

nraiorbit status my-film
nraiorbit run my-film --force shots      # redo shot planning and everything after it
nraiorbit library validate
nraiorbit library search action --tags fight --rasa veera --intensity 8
nraiorbit publish my-film --dry-run      # show YouTube metadata; uploads default to private
```

Out of the box, all generators are **mock** providers. A real run with Claude
therefore produces a real story, screenplay, cast, shot list and cue sheet, rendered as a
labelled-colour-card **animatic** with tone placeholders for voice and music. That lets
you review pacing before any GPU spend. Plug real generators into
`src/nraiorbit/providers/` and select them in `config/pipeline.yaml`
(see [docs/TOOLS.md](docs/TOOLS.md)).

## What is implemented vs. designed

| Area | In this repo now | Next to build |
|---|---|---|
| Agents | Story Analyst, Screenwriter (outline + scenes), Dialogue Critic loop, Casting (library-first + identity-pack briefs), Director, Music Director; structured outputs, caching, refusal fallback | Production Designer, vision QA critic, Lyricist |
| Planning | Structure templates, runtime rebalance + landmark checks, frame-exact shot fitting, rasa-driven cue planner | Teaser cut, song sequences |
| Asset library | Manifest schemas, registry, search, version-pinned lockfile, licence/consent validation, example manifests | Embedding search (pgvector/Qdrant), media in LFS/DVC |
| Guards | Identity guard (assignment, drift, swap, thresholds, retake loop wired into render), voice guard (speaker, Telugu CER, emotion, MOS), adapters for InsightFace and SpeechBrain | Body/costume guard, continuity critic, raga-conformance check, ASR/emotion adapters |
| Generators | Mock video/TTS/music (ffmpeg animatic); Veo shot-pack export + inbox import for video, voice and music; Indic Parler-TTS Telugu adapter (written from the model card, not yet run) | Veo API automation, ComfyUI keyframes + identity LoRA, voice conversion, lip-sync (AWS), stem-based score, SFX |
| Cost | `nraiorbit estimate`: Veo API/subscription, Claude, AWS lip-sync, storage, Mac time; actual Veo spend from the inbox | GPU-hour tracking per take |
| Post | Per-scene assembly, sidechain-ducked music, loudness master, dialogue/music stems for DaVinci Resolve, SRT te/en, trailer cutter | OTIO/FCPXML timeline export, thumbnails |
| Publish | YouTube metadata (chapters, disclosure), upload with captions | Scheduling / premieres |
| Infra | File-based stage cache, review gates, guard/cost logs, CI | Temporal workflows, GPU worker pools, review UI |

## Repository layout

```
config/       pipeline.yaml (providers, thresholds, gates), navarasa.yaml (emotion → music/voice/camera)
docs/         design documents
examples/     story prompts
library/      asset repository manifests (media lives in LFS/DVC/object storage)
src/nraiorbit/
  agents/     LLM agents          guards/     identity + voice guards
  planning/   runtime + music     providers/  generator interfaces + mock + embedders
  post/       assembly, trailer   publish/    YouTube
  prompts/    versioned agent prompts
  pipeline.py stage orchestrator  cli.py      `nraiorbit` command
tests/        unit tests + offline end-to-end demo
```

## Responsible use

Faces must be synthetic or from consenting performers. Voices must come from Telugu
voice artists under AI-voice contracts. Every music stem and SFX must be licensed for
commercial use, and every model's licence must allow commercial use of its outputs.
YouTube uploads always set the altered/synthetic-content disclosure. See the compliance
checklist in [docs/INFRA_COST_ROADMAP.md](docs/INFRA_COST_ROADMAP.md).
