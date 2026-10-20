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

## 7. Guard rails on cost (2026-10-17)

Filming is the expensive part, so the framework refuses to pay before it has looked. All of these are on by default
unless marked; none changes what an existing project renders.

- **The estimate before the paid stages.** `nraiorbit produce` prices the run first (Veo seconds at the billed clip
  lengths, images, voice characters, Lyria cues, sync.so seconds for every shot with a speaker in frame; cached work
  is free) and stops with `blocked:no cap` when `budget.max_usd` is 0 or unset, or `blocked:estimate` when the
  estimate plus what is spent would pass the cap. The numbers are in `reports/produce.json` (`steps.estimate`) and
  `nraiorbit run` prints the lip-sync line with the rest of the preview.
- **Publish only on a passing film check.** With `policy.publish` set, YouTube happens only when `qa film` is `ok`;
  otherwise the report says `held` and `nraiorbit release youtube <slug>` publishes after a look.
- **`--offline` covers music too.** A Lyria cue that is not cached (or in `inbox/music`) stops the run instead of
  paying; a lip-sync job that is not cached was already covered.
- **YouTube without a browser.** On a server the token must exist: `<project>/.youtube_token.json`, or the
  `YOUTUBE_TOKEN_JSON` secret that seeds it. `nraiorbit release youtube <slug> --auth` on a laptop makes it once.
- **`learn youtube` prices the watch first** from the video length (`--minutes`, or `--start/--end`, else a feature's
  150 min) and refuses above `--max-usd` (default $1) unless `--yes`. Every watch is written to
  `library/references/ledger.jsonl` with its token counts (`pricing.gemini_video` holds planning numbers; check them
  against ai.google.dev/pricing before budgeting a batch).
- **Lip-sync only where there is voice** (`lipsync.sync_silence`; template `auto`, framework default `always`): silent
  stretches of a speaking shot where the filmed mouth is already still are left as filmed, so they are not paid for;
  silence where the video model left the mouth moving is still sent, so a face never mouths words without a voice.
  `never` syncs only the lines and a margin. Measured on godavari10: 38 of the 400 seconds sent to sync.so had no
  voice (about $5 of $53). godavari10 itself stays on `always`: its paid results are keyed to the old crops.
- **Shots on the billable grid** (`shots.billable_grid`; template `true`, framework default `false`): with Veo, scenes
  land on even seconds and shots on 4, 6 or 8 s (8 s only at 1080p), adding up to the scene exactly, so no generated
  second is paid for and trimmed away; a scene whose shot count cannot meet it is fitted proportionally and noted in
  `reports/shot_issues.jsonl`. Measured on godavari10: 82 generated seconds were trimmed (about $6.50 at its mix of
  tiers).
- **Not done, and why:** a cheaper image model for faceless frames. The ledger shows 95 keyframes (every one a shot
  with a character in frame) and 39 identity images (faces); the pipeline makes no faceless images, so there is
  nothing to move to a cheaper model.

## 8. The control room (2026-10-17)

`nraiorbit ui` is one private web page over the bucket and the two queue sheets (deploy/ui/README.md): a story form
(slug, runtime, cap, gates, YouTube setting), a board of films with their state, pending gates, spend against cap and
the next estimate, a page per film with every stage's output (story bible, outline, scenes with every line in Telugu
and English and its delivery, cast with identity images, shot lists with first frames, render progress, QA, the film
and trailer playing in the page, delivery links, the YouTube result, the run report, the runner's log), and the
reference queue. Approve, retry, cap and "run a tick now" are the only buttons that change anything, and they write
only the queue sheet; the runner does the work under the row's cap. There is no database, so the page cannot
disagree with the runner. On Cloud Run it sits behind Identity-Aware Proxy with an allow-list of e-mail addresses,
and the app checks the address again. Locally: `nraiorbit ui --root projects --no-auth`.

Live progress (2026-10-19). The plan files land in the bucket only when a run returns, so a page that infers the
stage from them alone showed the story gate for 25 minutes while the runner wrote the outline and fifteen scenes. The
runner now says where it is, in two records the page reads: `<slug>/reports/progress.json` (phase, when it began,
last line, the execution's name, `state` running until the run ends) and `queue/running/<execution>.json` (what each
tick is doing: a film, a reference, the library, or nothing; removed when the tick exits). A stage is **executing**
only while the record says running and Cloud Run lists its execution as running; otherwise the record is ignored and
the inference from the plan files stands, so a crash never leaves a stage executing. The Runner tables show each
execution's doing, link to its console page, call a record whose execution ended "left behind", and mark two running
executions on one film as a duplicate to cancel. "Run a tick now" refuses while a tick runs.

What the runner learned at the same time: the shared library (identity packs, reference profiles) is restored from
the bucket before every tick and backed up after it, so a fresh container never pays again for images it has; the
gates' review marks are backed up with the project; the sheet's cap, gates and publish setting are written into the
project on every tick, so a change in the control room reaches the run; a story may bring
`<story>.overrides.yaml` (runtime, voice cast); a reference row carries the video's minutes for the cost guard.

## 9. Concurrency and tick overhead (2026-10-20)

Planning a film is mostly Claude calls made one after another, and every tick pays a fixed cost before it works.
Both were cut without trading away quality or money. Speed-ups that trade either are not built: the owner turned
them down (docs/PROJECT_STATUS.md, 2026-10-20).

**Shot lists run side by side.** The director writes one shot list per scene, and no call reads another scene's
shot list. So `llm.concurrency` of them run at once (`config/pipeline.yaml`, default 2; `src/nraiorbit/concurrency.py`).
The default reaches every film whose own `pipeline.yaml` does not set it. `1` restores the one-at-a-time order.

- Each call gets the same inputs as in the one-at-a-time order.
- The first call runs alone until its response starts streaming. From then on the prompt cache it wrote can be
  read, so the cache is written once and the bill stays the same.
- Results are cleaned, fitted, saved and logged on the main thread, in scene order. The plan files, reports and log
  are those of the one-at-a-time run; only the rows of `reports/llm_usage.jsonl` may land in another order.
- After an error no new call starts. Calls already running finish and are saved, then the first error is raised.
- A Ctrl-C in the CLI waits for the calls already running; the service is unaffected.
- The Claude client retries up to 6 times with back-off (`max_retries=6`): calls side by side meet rate limits more
  often.
- Each row of `reports/llm_usage.jsonl` also carries `rl_output_limit` and `rl_output_remaining`: the output-token
  rate limit and what was left of it, from the response headers.

**The rest stays one at a time.** Scenes, the dialogue critic and casting briefs run one at a time. Each scene is
written from the previous scene's synopsis, and the critic reviews it before the next scene starts. No casting brief
is saved until every one is written, so a brief running beside one that fails would be paid for, thrown away and
paid for again on the retry.

**Ticks.**

- A tick reads the two sheets first. With nothing to do it exits there: no library restore, no library backup.
- Restore and backup go through the storage client, set by set. Each set runs `NRAIORBIT_SYNC_WORKERS` transfers at
  once (default 8).
- A folder moves only the files whose crc32c differs, never judged by size alone.
- A download goes to a temporary file beside the target and is renamed over it only once complete. A failed
  download never touches the good local copy.
- The queue sheets are read through the client too. A sheet the client cannot read is read with gcloud, never
  taken as empty.
- Where no client can be made (no credentials on the machine), gcloud does the restore and backup as before.
- After a failed library restore, the library backup sends up only the files the bucket lacks (gcloud's
  `--no-clobber`). The disk then holds the image's older copies, which must not replace newer ones.
- A run that fails after its restore still backs up what it saved, such as a later scene's shot list or a paid clip.
- The runner's log has a timing line for every project restore and backup:
  `restore: <n> sets in <s> s (slowest <set> <s> s)`, and the same for `backup:`.
- Retry in the control room starts a tick, as Approve does.

## 10. Tools per stage (2026-10-20)

Every stage in the control room names the service or model that does its work: "Claude Opus 5.5" with the agent's
effort, "Veo 3.1 Fast 720p", "ElevenLabs eleven_v4", "Lyria 3 lyria-3-pro-preview", "sync.so sync-3", ffmpeg,
"YouTube Data API v3 (private)". `src/nraiorbit/tools.py` reads them from the film's resolved config. It follows the
pipeline's own rules and defaults but never imports the pipeline, since the control room only reads files.

- A film with its own `pipeline.yaml` is that file merged over `config/pipeline.yaml`, exactly what the runner sees.
- A queued film has no `pipeline.yaml` before its first tick. Its page shows what that tick will make: the project
  template, then the story's `.overrides.yaml`, then the row's cap, gates and publish. These tools are marked planned.
- A film made by hand without a `pipeline.yaml` runs on the defaults, and its page shows them as they are.
- A capability on a mock is named for what it stands in for: "picture: mock (no paid call)", "voices: mock (no paid
  call)", "music: mock (no paid call)", "lips: none". One sentence on the page says what renders on mocks, for
  example "Renders on mocks: picture, voices, music · lips: none. No paid render calls; Claude planning is still
  billed."
- `tools.is_animatic` is the pipeline's own rule: keyframe, video, tts and music all mock or none.
- A "Used:" line under a stage lists what was really called. Claude calls come from `reports/llm_usage.jsonl` (the
  model that served each call). Paid calls come from `reports/cost.jsonl` with their dollars; a refund nets out. A
  Claude model other than `llm.model` is a fallback the API served instead, shown in the warning colour.
- While a tick runs, the "Running" line at the top of the film page names the tools of the phase under way. The
  board shows each film's render tools, with a mock or planned badge.

