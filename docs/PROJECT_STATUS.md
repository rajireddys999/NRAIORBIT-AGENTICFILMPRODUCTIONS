# Project status

_Last updated: 2026-10-13. Branch: `claude/telugu-ai-movie-generator-yhb6po` (not merged into `main` yet)._

## 1. Completed

### Design (docs/)

| Doc | Contents |
|---|---|
| ARCHITECTURE.md | System overview, stages S0–S16, lockfile ("guard trail"), retake loop, storage layout |
| AGENTS.md | Claude agents, specialist scoring models, critique loops, feedback flywheel |
| CHARACTER_IDENTITY.md | Identity packs, conditioning, identity guard thresholds |
| VOICE_TELUGU.md | Dialects, voice bank, synthesis paths, voice guard, lip-sync |
| MUSIC_TELUGU.md | Navarasa → raga/tala/instruments, cue planning, mass-elevation recipe |
| ASSET_LIBRARY.md | Repository of characters, voices, music, actions, locations, SFX |
| TOOLS.md | Every tool/model per stage, with licence warnings and a starter stack |
| INFRA_COST_ROADMAP.md | Infrastructure, GPU and cost estimates, compliance, roadmap |
| POC_RUNBOOK.md | Step-by-step 5-minute POC and trailer test, with the bill |

### Pipeline code (src/nraiorbit/)

| Area | What works |
|---|---|
| Agents | Story Analyst, Screenwriter, Dialogue Critic, Casting, Director, Music Director (Claude Opus 5.5, structured outputs, caching, refusal fallback) |
| Planning | Telugu structure templates, runtime rebalance, frame-exact shot timing, rasa-driven cue plan |
| Asset library | Manifests, search, version-pinned cast lockfile, licence and consent checks, example characters/voices/stems/actions |
| Video | **Veo API** (keyframe mode: Gemini first frame → Veo; or reference mode), per-shot retakes (`nraiorbit retake`, earlier attempts kept), manual shot pack + inbox import, placeholder animatic |
| Images | **Gemini** keyframes and character identity packs (`nraiorbit identity build`) |
| Voice | **Gemini Telugu TTS** (voice per character, cached), recorded-line import, Indic Parler-TTS adapter (not yet run) |
| Guards | Identity guard (face assignment, drift, swap; retake loop) and voice guard, with InsightFace/SpeechBrain adapters |
| Post | Per-scene assembly, ducked music, −14 LUFS master, dialogue/music stems for DaVinci Resolve, te/en subtitles, trailer cutter |
| Cost control | `nraiorbit estimate`, cost preview + confirmation before paid runs, budget cap, `reports/cost.jsonl` ledger, `--only trailer`, no double payment on re-runs |
| Publish | YouTube metadata (chapters, AI disclosure, private by default) and upload code |
| Quality | 66 tests, including the full automatic POC and retakes against a fake Google client; CI workflow; lint clean |

### POC content

"Godavari Simham", 5 minutes, pre-written in Claude Code, so the POC needs no Claude API calls: 10 scenes, 54 shots (each ≤ 8 s), 13 Telugu dialogue lines, 9 music cues, 3 characters (Arjun, Meera, Bhairava). No minors appear in any shot, since Veo is set to generate adults only.

### Execution so far

- A cloud session was started with your `GEMINI_API_KEY` environment variable. It created the POC, ran the estimate and exported `DIALOGUE.md`.
- It stopped before any paid call: the environment's network settings block `pypi.org` and Google's API, and it needs your permission to use the key.
- Nothing has been spent yet.

### Verified vs. not verified

| Verified (here, with ffmpeg and a fake Google client) | Not verified yet |
|---|---|
| Every stage end to end, output files decode, loudness, subtitles, budget stop, safety-filter handling, caching, SDK signatures | Real Veo / Gemini image / Gemini TTS calls and model names on your key |
| | Claude API path for new stories (no Anthropic API key used) |
| | Indic Parler-TTS, InsightFace and SpeechBrain adapters, YouTube upload |

## Decision: Option A, quality first

Video through the **Google Veo API**, with no compromise on quality:
- **Video:** Veo 3.1 standard at native 1080p (8 s clips)
- **Images:** Gemini's pro image model for the character photos and the first frame of each shot
- **AWS:** lip-sync GPU and storage only

The 5-minute POC costs about $56 for the trailer test and about $177 in total. A full 1h45m
film costs about $4,500 per pass, or roughly $6,000–$9,000 with retakes.

The pipeline also has a manual hand-generation flow (`--manual`) and lower-cost tiers
(Fast/Lite) in configuration, but this project does not use them.

## 2. Next steps: the trailer test

| # | Step | Who | Cost |
|---|---|---|---|
| 1 | Environment → Network access: allow `pypi.org`, `files.pythonhosted.org`, `generativelanguage.googleapis.com` (keep "Allow package managers" ticked) | You | — |
| 2 | Confirm `GEMINI_API_KEY` is a Google AI Studio key (`AIza…`) on a project with billing; revoke the `veo3_pat_…` token that was pasted in chat | You | — |
| 3 | Start a new session and paste the prompt from [NEXT_SESSION_PROMPT.md](NEXT_SESSION_PROMPT.md). Tell it that it may run commands that use `GEMINI_API_KEY` | You | — |
| 4 | Key check (`nraiorbit google models`), POC setup, estimate, dialogue export | Session | free |
| 5 | Approve caps ($70 trailer test, $200 full film) and the 13 Telugu lines | You | — |
| 6 | `nraiorbit identity build godavari5`, then approve the three faces | Session, then you | ≈ $1.50 |
| 7 | `nraiorbit run godavari5 --only trailer`: 17 shots + 13 lines → trailer.mp4 | Session | ≈ $56 |
| 7b | `nraiorbit retake godavari5 <shot>` for any shot you don't like | Session | ≈ $3.33 per shot |
| 8 | `nraiorbit run godavari5`: the other 37 shots → full 5-minute movie | Session | ≈ $121 |
| 9 | `nraiorbit estimate godavari5`: actual spend → plan the feature (`actual ÷ 54 × ~1,400 shots`) | Session | free |

## 3. Still required for the full end-to-end pipeline

| Gap | Why it matters | Plan | What we need from you |
|---|---|---|---|
| First real Google run | Model names and parameters are only checked against the SDK | Steps 1–8 above | Network + key + approvals |
| **Lip-sync** | Mouths don't match the Telugu audio | LatentSync adapter on an AWS g6.xlarge (≈ $2 for the POC) | AWS paid plan + GPU quota request ("Running On-Demand G and VT instances"), budget alarm |
| **Music** | Cues are placeholder tones unless you supply tracks | Option A: Lyria music model through the same Google key. Option B: commissioned Telugu stems. Option C: your own tracks in `inbox/music/` | Choose A, B or C |
| Automatic quality retakes | Today: one attempt per shot; you judge faces | Identity guard with a face model on each clip | Licence choice: InsightFace is non-commercial; AWS Rekognition is commercial |
| Telugu quality | Dialogue was written by AI | Native review of every scene; recorded voices for leads | Your review, or a Telugu writer/voice artists with consent |
| DaVinci Resolve timeline | Today: movie + stems, not an editable timeline | OTIO/FCPXML export | — |
| Teaser, thumbnail | Not built | Teaser cutter (60 s), thumbnail from best keyframe | — |
| YouTube upload | Code exists, never run | Test upload as private | Google Cloud OAuth client + verified channel (needed for videos over 15 min) |
| New stories automatically | Story/screenplay agents need the Claude API | Use an Anthropic API key, or keep writing stories in Claude Code | API key from console.anthropic.com (optional) |
| Feature-length scale | 1h45m ≈ 1,400 shots, ≈ $4,500 per pass at Veo 3.1 standard 1080p (≈ $6,000–$9,000 with retakes) | Temporal workflows, review UI, music stem library, consented voice bank | Budget and timeline decision |
| Merge to main | Work is on a feature branch | Pull request | Say "open a PR" |
