# The framework: one story in, one film on YouTube out

This document says how the code is organised so that **every new story runs through the same machinery** and
nothing film-specific lives in Python. It also describes the always-on runner on Google Cloud that takes work from
CSV queues (stories to produce, public reference films to learn from) and the legal line it keeps.

## 1. Three layers

| Layer | Where | What goes there | Rule |
|---|---|---|---|
| **Framework** | `src/nraiorbit/` (the `nraiorbit` package), `config/` defaults, `tests/`, `deploy/` | Agents, providers (video, images, voices, music, lip-sync), planning, post-production, QA, release, backup, the service runner | Generic. Driven by configuration only. Every change comes with tests. |
| **Project** (one story, one film) | `projects/<slug>/` | The story input, `pipeline.yaml` overrides (providers, budget, voice cast, per-shot settings), approvals, every generated artifact and report | Data, never code. Git-ignored except the template; backed up to the bucket. |
| **Library** (reusable across films) | `library/` | Characters, voices, locations, actions, music motifs, SFX, **reference profiles** learned from public films | Manifests in git, media in the bucket. |

A per-film "script" is a smell: when a film needs something the framework cannot express, the framework gets the
option (with a test) and the film gets a line in its `pipeline.yaml`.

```
src/nraiorbit/
  agents/        story analyst, screenwriter, dialogue critic, casting, director, music director (prompts in prompts/)
  planning/      runtime planner, cue planner, shot scoring
  providers/     veo + gemini images + gemini tts (google_ai), lyria, elevenlabs, inbox, sync_so, cloud_lipsync, mock
  post/          ffmpeg, facelock (speaker lock), assemble (frame grid), cards, subtitles, trailer, speech (clarity)
  guards/        identity and voice guards
  qa/            lip-sync QA (strips, aperture metrics, blob scan) and the film check (length, loudness, voice placement, cuts)
  release/       deliverables (1080p, 720p, trailer), bucket upload, signed links, YouTube publish
  ops/           backup and restore of a project to the bucket
  service/       the queue runner and the YouTube reference learner
  pipeline.py    the stage DAG with review gates and the budget cap
  cli.py         one command per verb
```

## 2. The flow of one film

```
story.md ──> story_bible ──> outline ──> scenes ──> cast ──> shots ──> music_plan ──> render ──> assemble ──> trailer
                 │gate:story             │gate:cast     │gate:faces   (paid: images, Veo, voices, music, lip-sync)
                                                                        └─> qa ──> release ──> publish(YouTube) ──> backup
```

| Step | Command | Human gate | Paid |
|---|---|---|---|
| Create the project from the template | `nraiorbit new <slug> --story story.md` | | |
| Plan (bible, outline, scenes, cast, shots, cues) | `nraiorbit run <slug> --until shots` | story, cast (auto-approvable per policy) | Claude only |
| Faces | `nraiorbit identity build`, `nraiorbit keyframes` | **faces approved before any video is paid** | images |
| Voices | `providers.tts: elevenlabs` with `voice.cast` | lines approved | characters on the plan |
| Picture, music, lip-sync | `nraiorbit run <slug> --yes --max-spend N` | cost preview, cap | Veo, Lyria, sync.so |
| QA | `nraiorbit qa lipsync <slug>`, `nraiorbit qa film <slug>` | fixes go to `lipsync.shots` | |
| Deliver | `nraiorbit release deliver <slug>` | | storage pennies |
| Publish | `nraiorbit release youtube <slug> [--trailer] [--privacy private]` | the owner flips private to public in YouTube Studio | |
| Keep | `nraiorbit backup <slug>` | | storage pennies |

`nraiorbit produce <slug>` runs the whole row with the gates set by the project's `policy` (ask, auto, or stop) and
writes a one-page QA and cost report at the end. The runner never raises a budget cap on its own.

## 3. Where the one-off scripts went

| Scratchpad script (godavari10) | Now |
|---|---|
| `film_versions.py`, `eleven.py`, `install_takes.py` | `providers/elevenlabs.py`: `providers.tts: elevenlabs`, `voice.cast: {character: voice_id}`, delivery tags from the script, optional time-matching, cost estimate per character count |
| `configure_sync3.py` | `pipeline.yaml` lines (`providers.lipsync: sync_so`, `lipsync.model`), nothing to run |
| `qa_sync3.py`, `strips.py`, `qa_crops.py`, `blobs.py` | `qa/lipsync.py` → `nraiorbit qa lipsync <slug>` writes strips, metrics and a summary under `reports/qa/` |
| `film_check.py` | `qa/film.py` → `nraiorbit qa film <slug>` |
| `deliver.py`, `sign_urls.py`, `deliver_parts.py` | `release/deliver.py` → `nraiorbit release deliver <slug> [--parts N]` |
| `backup_g10.sh`, rsync one-liners | `ops/backup.py` → `nraiorbit backup <slug>` and `nraiorbit restore <slug>` |
| `render_free.py`, `assemble_free.py`, `rebuild_free.py` | `nraiorbit run <slug> --force render --offline` (lip-sync never starts a paid job) |

## 4. Project layout (the template)

```
projects/<slug>/
  input/story.md            the story, any language
  pipeline.yaml             overrides only: providers, budget.max_usd, voice.cast, lipsync.shots, policy
  pricing.yaml              optional price overrides
  cast.lock.yaml            written by the casting agent, edited by the owner
  takes.yaml, reuse.yaml    which take of a shot is used; clips reused from another project
  inbox/dialogue, inbox/video, inbox/music     hand-made or externally made assets that beat generation
  01_story_bible.json … 11_master/             stage outputs (regenerated; never edited by hand)
  reports/                  cost.jsonl (the ledger), lipsync.jsonl, review_queue.jsonl, qa/
  reviews/                  <gate>.pending / <gate>.approved
```

`nraiorbit new` copies `projects/_template` and writes the story. The template's `pipeline.yaml` carries the
choices this film settled: Veo 3.1 tiers by shot kind, Gemini 3 Pro Image keyframes checked against approved faces,
ElevenLabs voices, Lyria score, sync.so sync-3 lip-sync through the speaker lock, frame-grid assembly, a $0 budget
until the owner sets one.

## 5. The always-on runner on Google Cloud

A Cloud Run **job** (CPU only; the paid models are APIs) wakes every 15 minutes from Cloud Scheduler, reads two CSV
queues in the private bucket, does one unit of work per row that is ready, writes the row's state back, and exits.
No GPU, no idle cost; a run is billed per second of CPU (a 10-minute film takes about 2 to 3 CPU-hours end to end,
under $1).

```
gs://<bucket>/queue/stories.csv       slug, story_uri, budget_usd, policy, youtube_privacy, state, note
gs://<bucket>/queue/references.csv    url, title, what_to_learn (score|emotion|action|characters|trailer|all), state, profile_uri
```

| Row state | Meaning | Who moves it |
|---|---|---|
| `new` | queued | owner (edit the CSV or `nraiorbit service add`) |
| `planning`, `awaiting:<gate>` | stages running; stopped at a gate | the job; the owner approves with `nraiorbit approve` or a cell edit |
| `producing` | paid stages under the row's cap | the job |
| `qa`, `released`, `published` | report written; files in `final/`; YouTube id in `note` | the job |
| `blocked:<reason>` | budget hit, API refused, credits out | the job; the owner fixes the cause and sets `new` again |

The runner holds the keys as Secret Manager secrets (Google service account, ElevenLabs, sync.so, the YouTube OAuth
token made once on a laptop with `nraiorbit release youtube --auth`). The same cap and ledger rules apply: nothing is
paid past `budget_usd`, and a row never raises its own cap.

### Reference learning (the "make it better" loop)

`nraiorbit learn youtube <url>…` asks Gemini on Vertex AI to **watch a public YouTube link** and write a
**reference profile**: how the score is built (cue anatomy, where silence sits, which instruments carry which rasa),
how emotional beats are paced, how action is staged and cut, the archetypes and screen presence of hero, heroine,
villain and supporting cast, how the trailer is structured. Profiles are JSON under `library/references/<id>/` and
the story, casting, music and trailer agents get the relevant ones as **examples** in their context.

The legal line, kept by code: the learner never downloads a video, never extracts frames, voices or music, never
feeds a reference clip to a generator, and never reproduces a named person's likeness or voice. It learns
patterns and writes them down in words; our films are generated from our own story, our own characters and our own
voices. Reference profiles are tagged with their source URL so a profile can be removed on request.

## 6. What stays the owner's decision, by design

Approving faces before video is bought, the voice cast, the budget cap per film, the final line of credits, and
flipping a YouTube upload from private to public. Everything else is automated, logged and reversible.
