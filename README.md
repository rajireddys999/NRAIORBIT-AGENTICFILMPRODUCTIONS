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
| [docs/FRAMEWORK.md](docs/FRAMEWORK.md) | **How the code is organised:** framework vs project vs library, one story to YouTube, the always-on runner |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | System overview, stages S0–S16, lockfile, retake loop, storage layout |
| [docs/AGENTS.md](docs/AGENTS.md) | The decision-making agents, specialist models, critique loops, feedback flywheel |
| [docs/CHARACTER_IDENTITY.md](docs/CHARACTER_IDENTITY.md) | Identity packs, conditioning, the identity guard and its thresholds |
| [docs/VOICE_TELUGU.md](docs/VOICE_TELUGU.md) | Dialects, voice bank, three synthesis paths, voice guard, lip-sync |
| [docs/MUSIC_TELUGU.md](docs/MUSIC_TELUGU.md) | Navarasa mapping, cue planning, mass elevation recipe, composer engine, music guard |
| [docs/ASSET_LIBRARY.md](docs/ASSET_LIBRARY.md) | The asset repository: layout, manifests, retrieval, building it in 90 days |
| [docs/TOOLS.md](docs/TOOLS.md) | Every tool and model per capability, with licence warnings and a starter stack |
| [docs/INFRA_COST_ROADMAP.md](docs/INFRA_COST_ROADMAP.md) | Deployment, GPU hours and cost estimates, compliance checklist, phased roadmap |
| [docs/POC_RUNBOOK.md](docs/POC_RUNBOOK.md) | **Start here:** the 5-minute POC on a Mac + Veo + DaVinci Resolve, with its bill |
| [docs/GOOGLE_CLOUD_SETUP.md](docs/GOOGLE_CLOUD_SETUP.md) | Google Cloud account, billing, API key, budget alert and GPU quota, step by step |
| [docs/PROJECT_STATUS.md](docs/PROJECT_STATUS.md) | What is done, next steps, and what is still needed end to end |
| [docs/NEXT_SESSION_PROMPT.md](docs/NEXT_SESSION_PROMPT.md) | Copy-paste prompt to continue in a new Claude Code session |

## 5-minute POC and trailer test (automatic, quality-first: Veo 3.1 standard, 1080p)

```bash
pip install -e ".[dev,google]" && export GEMINI_API_KEY=...
nraiorbit poc godavari5                       # screenplay + 54 shots + cue sheet, already written
nraiorbit estimate godavari5                  # the bill before spending anything
nraiorbit identity build godavari5            # character reference images (~$1.50)
nraiorbit run godavari5 --only trailer        # trailer test: 17 Veo 3.1 shots at 1080p, ~$56, asks first
nraiorbit retake godavari5 sc002_s005         # redo one shot (~$3.33), earlier attempts are kept
nraiorbit run godavari5                       # full 5 minutes, ~$121 more (trailer shots reused)
```

Every run shows a cost preview and asks before spending. A budget cap stops it before
the limit, every paid call is logged to `reports/cost.jsonl`, and nothing is paid for
twice. Full steps, prices and sources are in [docs/POC_RUNBOOK.md](docs/POC_RUNBOOK.md);
`--manual` gives a hand-generation flow for any Veo plan. `nraiorbit estimate --runtime
105` estimates a full feature.

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

## One story to YouTube, with the tools the last film settled on

```bash
nraiorbit new rain-film --story stories/rain.md       # copies projects/_template: Veo 3.1, Gemini images, ElevenLabs
                                                      # voices, Lyria, sync.so sync-3; set budget.max_usd and voice.cast
nraiorbit produce rain-film --max-spend 150           # plan → faces → render → QA → deliver → (YouTube) → backup,
                                                      # stopping at each review gate; --gates auto approves all but cost and the upload
nraiorbit approve rain-film faces && nraiorbit produce rain-film
nraiorbit qa lipsync rain-film                        # frame strips and metrics for every speaking shot
nraiorbit qa film rain-film                           # length, loudness, voice placement, cuts on the frame grid
nraiorbit release deliver rain-film --tag v1          # 720p copy, bucket upload, 7-day links
nraiorbit release youtube rain-film --privacy private
nraiorbit backup rain-film                            # caches, inbox, reports, master, plan files → the bucket
nraiorbit learn youtube https://youtu.be/<id> --learn score --minutes 150   # craft notes from a public film, for
                                                      # the agents; priced first, refused over --max-usd (default $1)
nraiorbit service tick --stories gs://<bucket>/queue/stories.csv --references gs://<bucket>/queue/references.csv
nraiorbit ui --root projects --no-auth               # the control room on a laptop; on Cloud Run: deploy/ui/setup.sh
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
| Generators | **Veo API** (keyframe or reference mode), **Gemini keyframes**, **Gemini Telugu TTS**, Gemini identity-pack builder; shot-pack export + inbox import for hand-made clips, recordings and music; mock animatic; Indic Parler-TTS adapter (not yet run) | Lip-sync (Google Cloud L4 GPU), music generation, voice conversion, ComfyUI + LoRA path, SFX |
| Spend control | Cost preview + confirmation, budget cap, `reports/cost.jsonl` ledger, `--only trailer`, resumable without double payment | Per-provider rate limits |
| Cost | `nraiorbit estimate`: Veo API/subscription, Claude, cloud GPU lip-sync (Google Cloud or AWS), storage, Mac time; actual Veo spend from the inbox | GPU-hour tracking per take |
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
