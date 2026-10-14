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
| GOOGLE_CLOUD_SETUP.md | Google Cloud account, paid billing, project, API key, budget alert, GPU quota |

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

- 2026-10-13: the new paid `GEMINI_API_KEY` works (`nraiorbit google models` lists Veo 3.1, the pro image model and TTS).
  But the AI Studio project uses **Gemini API prepaid billing with $0 credit**, so Gemini API calls fail with
  `402 prepayment credits are depleted` (nothing charged).
- The Google Cloud billing account has **$300 free-trial credit** (valid to January 2027). To spend it, the POC now runs
  through **Vertex AI** with a service account (`nraiorbit-agentic-film-product@…`, project `nraiorbit-agentic-film-product`):
  - Project `pipeline.yaml`: `google.vertex: true`, `veo_model: veo-3.1-generate-001`, `tts_model: gemini-2.5-flash-tts`
    (Vertex names); `image_model: gemini-3-pro-image` needs `GOOGLE_CLOUD_LOCATION=global`.
  - Sign-in: `GOOGLE_APPLICATION_CREDENTIALS_JSON` (key JSON text) or `GOOGLE_APPLICATION_CREDENTIALS` (file path),
    plus `GOOGLE_CLOUD_PROJECT` and `GOOGLE_CLOUD_LOCATION=global`.
  - Network allow-list also needs `oauth2.googleapis.com`, `aiplatform.googleapis.com`,
    `us-central1-aiplatform.googleapis.com`, `www.googleapis.com`.
  - To do: the service account has the **Owner** role for now; change it to `roles/aiplatform.user`. The key file
    passed through chat, so replace it with a new key stored in the environment settings.
- **Character photos built and approved by you:** 11 images for $1.47 (`reports/cost.jsonl`). Check on the Billing → Credits
  page that the $300 credit paid for them.
- You approved the 13 Telugu lines and the trailer test ($70 cap).
- **Trailer test done (2026-10-13, Vertex AI):** 17 Veo 3.1 clips at 1080p, 13 keyframes, 13 Telugu TTS lines ->
  `11_master/trailer.mp4` (32 s, -13.4 LUFS) and `movie.mp4` (5 min; the other 37 shots are placeholder cards).
  No shot was blocked or paid twice. Ledger: $56.18 for the run, $57.65 in total. The ledger counts Veo at the
  with-audio rate ($0.40/s); Veo audio is off on Vertex, so the real bill should be lower. Check it on the Billing page.
- A pre-flight review before the paid run found and fixed: lost/double-paid Veo jobs on interrupted polling
  (now logged at submit and resumed from `inbox/video/.pending/`), no retries for images/TTS, uncaught Veo
  operation errors, unused Veo audio billed on Vertex, a poll with no timeout.
- Not approved yet: the full 5 minutes (about $121 more).
- **Voice and music upgrade (v2, 2026-10-14)**, after you reported the trailer voice "breaking" and a score with no elevation.
  Causes measured:
  - 9 of 15 trailer cuts chopped a line.
  - A movie line lost 5.4 s at a scene end, and two lines overlapped by 3.7 s.
  - The slow TTS prompt put English notes next to the Telugu.
  - The music was placeholder tones.

  Fixed:
  - Whole lines everywhere, and an AUDIO PROFILE / DIRECTOR'S NOTES / TRANSCRIPT TTS prompt with shorter pauses.
    You picked voice A, gemini-2.5-flash-tts, from an audition of 3 models.
  - A Lyria 3 Pro provider with style presets researched from Telugu mass scoring and frisson studies, scored to the
    picture (the hit lands on the hero-reveal shot, aligned in the audio). From an audition of 6 pieces you picked
    `epic_mythic` for elevation and `alaap_choir` for emotion.
  - Lyria needs response_modalities [AUDIO, TEXT]. It once returned no audio for the trailer cue ($0) and worked on a retry.

  Cost of v2: $7.54 on the ledger (cap $10). Total: $65.19.

### Verified vs. not verified

| Verified (here, with ffmpeg and a fake Google client) | Not verified yet |
|---|---|
| Every stage end to end, output files decode, loudness, subtitles, budget stop, safety-filter handling, caching, SDK signatures | Lip-sync, music generation, InsightFace/SpeechBrain adapters, YouTube upload |
| Real Veo 3.1, Gemini pro image and Gemini TTS calls on Vertex AI (trailer test, 2026-10-13) | |
| | Claude API path for new stories (no Anthropic API key used) |
| | Indic Parler-TTS, InsightFace and SpeechBrain adapters, YouTube upload |

## Decision: Option A, quality first

Video through the **Google Veo API**, with no compromise on quality:
- **Video:** Veo 3.1 standard at native 1080p (8 s clips)
- **Images:** Gemini's pro image model for the character photos and the first frame of each shot
- **Google Cloud for everything:** the Gemini API key, Cloud Storage backups and the
  lip-sync GPU in one project, on one bill ([GOOGLE_CLOUD_SETUP.md](GOOGLE_CLOUD_SETUP.md))

The 5-minute POC costs about $56 for the trailer test and about $177 in total. A full 1h45m
film costs about $4,500 per pass, or roughly $6,000–$9,000 with retakes.

The pipeline also has a manual hand-generation flow (`--manual`) and lower-cost tiers
(Fast/Lite) in configuration, but this project does not use them.

## 2. Next steps after the trailer test (2026-10-13)

The project media is not in git. It is saved as a backup (14 parts in chat, plus the Google Cloud Storage bucket
`gs://nraiorbit-agentic-film-product-media` once `storage.googleapis.com` is allowed). Restore it before any run;
otherwise paid clips would be generated again. Setup and the copy-paste prompt: [NEXT_SESSION_PROMPT.md](NEXT_SESSION_PROMPT.md).

| # | Step | Cost |
|---|---|---|
| 1 | You: new service account key (delete `71e11ffe…`), roles Vertex AI User + Storage Admin instead of Owner, env settings, network allow-list | — |
| 2 | Restore the backup; free trailer rebuild with `--max-spend 57.80`; Cloud Storage backup | ≈ $0.02/month |
| 3 | Full 5 minutes: `nraiorbit run godavari5 --yes --max-spend 200` | ≈ $121 on the ledger |
| 4 | Retakes of shots you pick | ≈ $3.33 each |
| 5 | Music: done (Lyria 3 Pro, styles picked); re-score a cue in another style if wanted | ≈ $0.50 per piece |
| 6 | Lip-sync on a Google Cloud L4 GPU | ≈ $1.50 |

## 2a. Earlier plan: the trailer test

| # | Step | Who | Cost |
|---|---|---|---|
| 1 | Environment → Network access: allow `pypi.org`, `files.pythonhosted.org`, `generativelanguage.googleapis.com`, `aiplatform.googleapis.com` (keep "Allow package managers" ticked) | You | — |
| 2 | Set up Google Cloud and the `GEMINI_API_KEY` key (`AQ.…` or `AIza…`) with [GOOGLE_CLOUD_SETUP.md](GOOGLE_CLOUD_SETUP.md) steps 1–7; revoke the `veo3_pat_…` token that was pasted in chat | You | — |
| 3 | Start a new session and paste the prompt from [NEXT_SESSION_PROMPT.md](NEXT_SESSION_PROMPT.md). Tell it that it may run commands that use `GEMINI_API_KEY` | You | — |
| 4 | Key check (`nraiorbit google models`), POC setup, estimate, dialogue export | Session | free |
| 5 | Approve caps ($70 trailer test, $200 full film) and the 13 Telugu lines | You | — |
| 6 | `nraiorbit identity build godavari5`, then approve the three faces (**done 2026-10-13, $1.47, faces approved**) | Session, then you | ≈ $1.50 |
| 7 | `nraiorbit run godavari5 --only trailer`: 17 shots + 13 lines → trailer.mp4 (**done 2026-10-13**) | Session | ≈ $56 |
| 7b | `nraiorbit retake godavari5 <shot>` for any shot you don't like | Session | ≈ $3.33 per shot |
| 8 | `nraiorbit run godavari5`: the other 37 shots → full 5-minute movie | Session | ≈ $121 |
| 9 | `nraiorbit estimate godavari5`: actual spend → plan the feature (`actual ÷ 54 × ~1,400 shots`) | Session | free |

## 3. Still required for the full end-to-end pipeline

| Gap | Why it matters | Plan | What we need from you |
|---|---|---|---|
| First real Google video run | Done: trailer test on Vertex AI (2026-10-13) | Steps 1–8 above | Network + key + approvals |
| **Lip-sync** | Mouths don't match the Telugu audio | LatentSync adapter on a Google Cloud g2-standard-4, one L4 GPU (≈ $1.50 for the POC) | Paid Google Cloud account + L4 GPU quota in asia-south1 ([GOOGLE_CLOUD_SETUP.md](GOOGLE_CLOUD_SETUP.md) step 8) |
| **Music** | Cues are placeholder tones unless you supply tracks | Option A: Lyria music model through the same Google key. Option B: commissioned Telugu stems. Option C: your own tracks in `inbox/music/` | Choose A, B or C |
| Automatic quality retakes | Today: one attempt per shot; you judge faces | Identity guard with a face model on each clip | Licence choice: InsightFace models are non-commercial (buy a licence and run it on the Google Cloud GPU), or AWS Rekognition (Google Cloud has no face-matching API) |
| Telugu quality | Dialogue was written by AI | Native review of every scene; recorded voices for leads | Your review, or a Telugu writer/voice artists with consent |
| DaVinci Resolve timeline | Today: movie + stems, not an editable timeline | OTIO/FCPXML export | — |
| Teaser, thumbnail | Not built | Teaser cutter (60 s), thumbnail from best keyframe | — |
| YouTube upload | Code exists, never run | Test upload as private | Google Cloud OAuth client + verified channel (needed for videos over 15 min) |
| New stories automatically | Story/screenplay agents need the Claude API | Use an Anthropic API key, or keep writing stories in Claude Code | API key from console.anthropic.com (optional) |
| Feature-length scale | 1h45m ≈ 1,400 shots, ≈ $4,500 per pass at Veo 3.1 standard 1080p (≈ $6,000–$9,000 with retakes) | Temporal workflows, review UI, music stem library, consented voice bank | Budget and timeline decision |
| Merge to main | Work is on a feature branch | Pull request | Say "open a PR" |
