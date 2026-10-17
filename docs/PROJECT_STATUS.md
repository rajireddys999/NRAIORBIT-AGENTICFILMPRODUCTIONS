# Project status

_Last updated: 2026-10-15. Branch: `claude/telugu-ai-movie-generator-yhb6po` (not merged into `main` yet)._

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
| Music | **Lyria 3 Pro** scored to the picture, style presets (`config/music_styles.yaml`), hit aligned to the hero shot |
| Lip-sync | **LatentSync 1.6** on a Google Cloud L4 GPU (`latentsync_cloudrun` provider; Vertex endpoint, Vertex job or Cloud Run runner), cached, endpoint always deleted after the batch. **Speaker lock** (`post/facelock.py`, MediaPipe BlazeFace): LatentSync sees only the speaker; only the speaker's mouth and jaw change; the skin texture is restored; a wrong-face check guards reused results; per-shot review fixes; offline rebuilds |
| Guards | Identity guard (face assignment, drift, swap; retake loop) and voice guard, with InsightFace/SpeechBrain adapters |
| Post | Per-scene assembly, ducked music, −14 LUFS master, dialogue/music stems for DaVinci Resolve, te/en subtitles, trailer cutter |
| Cost control | `nraiorbit estimate`, cost preview + confirmation before paid runs, budget cap, `reports/cost.jsonl` ledger, `--only trailer`, no double payment on re-runs |
| Publish | YouTube metadata (chapters, AI disclosure, private by default) and upload code |
| Quality | 120 tests, including the full automatic POC and retakes against a fake Google client; CI workflow; lint clean |

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
- **Final trailer (v3, 2026-10-14)**, after you reported talking faces with no voice, text appearing mid-trailer, and asked
  "how did we miss the lip sync". Lip-sync was a known missing step that should have been flagged before the trailer.
  Fixed:
  - **Lip-sync stage:** every filmed shot where a character speaks is lip-synced to that character's own lines with
    LatentSync 1.6 (silence elsewhere, so the mouth closes). 7 shots done. Worker image built by Cloud Build:
    `us-central1-docker.pkg.dev/nraiorbit-agentic-film-product/nraiorbit/latentsync:v2`.
  - **No silent talking faces:** the trailer uses a shot whose speaker is in frame only when it carries that line.
    New Veo prompts say "everyone else keeps their mouth closed" / "nobody is talking" for future shots.
  - **No stray text:** the title is a designed card (gold serif over a blurred, darkened frame), not plain text.
  - QA: 12 clips + title, 0 talking-face problems, both carried lines on lip-synced clips, −14.8 LUFS.
  - GPU quota found: Cloud Run GPU (non-zonal) = 0, Vertex training L4 = 0, **Vertex serving L4 = 2 in us-central1**,
    so the runner is `vertex_endpoint`. It takes about 25 min to deploy and 8–10 min per 1080p shot.
  - Licence: LatentSync uses InsightFace face models (non-commercial). Check before a commercial or YouTube release.

  Cost of v3: $3.42 (lip-sync $0.72 test + $2.21 final, Lyria trailer piece $0.50). **Total: $68.61** on the ledger.
  GPU and Lyria prices are conservative placeholders and Veo is counted with audio, so the real bill should be lower.
- **Storage:** everything is in the private bucket `gs://nraiorbit-agentic-film-product-media` (us-central1, public
  access blocked, about $0.02/month): `godavari5/backup/` (base + update v2 + update v3 backups with sha256) and
  `godavari5/11_master/` (original trailer, movie, subtitles, stems). Files under `lipsync/` are staging and are
  deleted after 7 days.

- **10-minute short film "godavari10" (Plan A, 2026-10-14)**: the owner chose a short film of the whole story
  instead of a feature, within $200 including GST ($115 hard stop on the ledger for this stage).
  - Planned with agents: a full 2h30m blueprint (40 sequences, 23 goosebump moments; about $8,300 expected at Veo 3.1
    standard, which is why the feature is out of reach on $200), then a 10-minute version in 13 sequences (620 s,
    133 shots, 58 Telugu dialogue blocks, 20 Lyria cues). The owner approved every Telugu line; Bhairava's line was
    changed to avoid a famous catchphrase; AI voices for all lines (crowd chant layered from 6 Gemini voices; the
    lullaby hums moved into the score).
  - 11 new adult characters (Satti, Veeranna, Kaasi, the mother Lakshmamma, Rudra, Chitti, and flashback versions;
    Raghavaiah built from Arjun's face), approved by the owner. No children on screen anywhere.
  - Video mix chosen from a side-by-side pilot: Veo 3.1 Fast 720p for faces, fights and crowds, Lite 720p for faceless
    wides, standard for 28 goosebump peaks; 14 godavari5 clips reused. All 81 first frames were checked against the
    approved faces before any video was paid (3 wrong Arjun faces and 1 wrong Veeranna were remade).
  - 7 shots were blocked by Veo's safety filter (not billed): knives, impact wording, a child's point of view, and
    blood-like stains. All were softened and regenerated. One clip had a TV-style "24" logo drawn from the words "24
    years ago" in the prompt; that wording was removed from all prompts and the clip retaken.
  - Cards: Telugu + English title at 1:15, INTERVAL at 4:21 and an end card (Noto Serif Telugu; install the font, see
    src/nraiorbit/post/cards.py for the paths searched).
  - First cut: $88.91 on the ledger (real Vertex prices since the price-table fix). Media backed up to
    `gs://nraiorbit-agentic-film-product-media/godavari10/` and `library/characters/*/refs`.
  - **Clear dialogue (2026-10-15):** every take levelled to one speaking level, then a clarity pass on the finished
    mix lifts any line under +12 dB over the music. Before, the median line was +5.8 dB over the music; after, every
    line is about +12 dB.
  - **Lip-sync and its QA (2026-10-15):** all 34 on-camera speaking shots went through LatentSync on one Vertex L4
    batch ($4.12). One shot, where Kaasi turns to profile, could not be done; with `on_failure: keep_original` it keeps
    its clip. A frame-by-frame review of 36 before/after sheets (every finding re-checked by a second reviewer) found
    LatentSync animating the **wrong face** in 9 multi-face shots, plus ghost blobs, smears and softness.
    The fix is the **speaker lock** (docs/VOICE_TELUGU.md §5.1):
    - LatentSync now sees only the speaker, and only the speaker's mouth and jaw change.
    - All 34 speaker choices were checked by eye.
    - 3 shots were remade with the face checked first:
      - sc008_s006: a burned-in "24 mm" camera overlay;
      - sc002_s011: a stranger standing where Chitti should be;
      - sc004_s003: a raised fist over the mouth.
    - 2 shots are kept as filmed (sc005_s003, sc006_s002), and 5 have frame-level fixes in `lipsync.shots`.
    - Ledger after the 3 remakes: $96.02. A GPU pass for the speaker crops of 9 shots followed: $1.15, ledger $97.17.
  - **Lip-sync QA round 2 (2026-10-15):** a second frame-by-frame review of all 34 synced shots (every high finding
    checked by two skeptics) found background blobs from the mouth patch (it reached past turned faces onto water,
    collars and blouses), a patch on the cheek of three-quarter faces, unsynced gaps, mouths moving in silence before
    or after a line, and melts. Fixed without any GPU job (ledger unchanged at $97.17):
    - **Picture and sound in sync:** every cut now sits on the film's frame grid. Rounding each clip on its own had put
      the voice up to 92 ms behind the lips by the last reel (125 ms at the end). The master keeps every frame (a remux
      used to cut its last 0.2 s).
    - **Face-mesh paste-back** (`lipsync.mesh`, MediaPipe Face Landmarker): the patch sits on the real lips and is
      clipped to the face outline, so LatentSync's smears beside the face never show.
    - **Free per-shot fixes** in `lipsync.shots`, all reusing the paid lip-sync: `filmed_frames`, `fade_frames`,
      `repair_frames`, `bridge_gaps`, `extend_track`, `protect`, `sync_s` (cut a shot right after its line) and
      `cut_in_s` (start a shot at its line). 16 shots got new settings in round 2.
    - Re-timed inside their scenes (scene lengths and music unchanged): sc001_s002, sc003_s005 and sc008_s006 end soon
      after their line; sc004_s003 starts at Arjun's voice instead of 0.25 s of silent shouting.
    - sc006_s002 (Bhairava close-up) is lip-synced again from the paid result.
    - **Round 3 (verification, 2026-10-16):** every changed shot was compared before/after frame by frame (13 shots,
      all better, nothing high) and the whole film re-measured (14880 frames, every cut on its frame, voice 5 ms
      behind the lips everywhere, i.e. one eighth of a frame; trailer fine). Four more free tweaks came out of it
      (sc001_s002 fade, sc006_s002 joins, sc011_s004 repair, sc013_s003 shorter repair so the 'm' closes). The
      completeness critic's remaining free options need the owner: a cutaway over sc009_s006 f48-77 (her line
      off-screen instead of a closed mouth), an audio re-time for sc011_s006 f104-109, a sound edit (exhale) for
      sc007_s004 f152-174.
    - **Final deliverables (2026-10-16):** gs://nraiorbit-agentic-film-product-media/godavari10/final/ holds the full
      film as one file in 1080p (742 MB, the master) and 720p (165 MB) and the trailer; the owner got 7-day signed
      download links. The working master stays under godavari10/11_master/. godavari10/final/source/ holds every
      picture source of the film with a manifest: the 84 generated keyframes, frame 0 of all 133 clips, the 133 Veo
      clips used plus the 34 lip-synced versions, and the cast reference photos (loose files and four zips).
  - **Voice test (2026-10-17):** the owner compared the trailer's 5 lines in Gemini 2.5 Flash (current), Gemini 2.5 Pro
    TTS, Chirp 3 HD Telugu, ElevenLabs v3 (stock voices) and ElevenLabs Eleven v4 with native Telugu library voices
    (E: Sai Datta/Seeta cast, F: Srinivas/Vijaya cast). Then the whole film was re-voiced in E and F (55 lines each,
    one library voice per character, picture, music and lip-sync untouched; takes time-matched to the old lines where
    within 12-18 %): gs://.../godavari10/final/voice_test/ (1080p + 720p per version, lines_E.csv / lines_F.csv list
    voice and timing per line). Not yet chosen; the chosen voice still needs the 34 speaking shots re-lip-synced
    (LatentSync about $5, or sync.so lipsync-2-pro about $25-40). ElevenLabs costs sit on the owner's Creator plan
    (included characters); the ledger carries them as estimates ($98.61 incl. the voice tests). The ElevenLabs key is
    the environment secret ELEVENLABS_API_KEY; api.elevenlabs.io is on the environment's allowed hosts.
  - **sync.so pre-flight (2026-10-17, free):** `SYNC_API_KEY` is accepted by api.sync.so, the model ids `sync-3` and
    `lipsync-2-pro` pass its validation, and the service account signs the staging links. The 34 speaking shots give
    36 speaker-crop segments, 198.8 s in all: **sync-3 $26.44** (ledger $98.61 → $125.05) or **lipsync-2-pro $16.50**
    (→ $115.11); either needs the $115 hard stop raised first. The E and F takes are backed up under
    gs://.../godavari10/final/voice_test/takes_E and takes_F, the two switch scripts under godavari10/project/scripts/.
    The provider remembers every submitted generation (cache/lipsync/<key>.pending.json) and polls it again on the next
    run, so a crash, a timeout or a result download the network refuses is never paid twice. The host sync.so serves
    results from is not known yet: if the environment's network policy refuses it, the first job's error names it,
    the owner allows it, and the next run downloads the paid result.
  - **Voice E lip-sync with sync.so sync-3 (2026-10-17):** the owner chose voice E (ElevenLabs Eleven v4: Sai Datta,
    Bharadwaj, Seeta, Madhavi, Srinivas and the rest, one library voice per character) and sync-3. The E takes went
    into **inbox/dialogue/<line_id>.wav** (time-matched where the voice test had fitted them, so the QA'd cut keeps its
    timing), the earlier inbox takes to inbox/dialogue_before_E/. All 34 on-camera speaking shots were lip-synced with
    sync-3 through speaker crops (36 segments, 198.8 s, $26.44), including the Kaasi profile shot LatentSync could not
    do; nothing left unsynced.
    - **A costly mistake first ($26.44):** the first sync-3 pass ran with `--force render`, which wipes 05_dialogue and
      regenerates every line from inbox/dialogue or the TTS cache. The E takes had been copied into 05_dialogue, so
      the pass was made on the old Gemini voices. Caught by checking the timeline's line lengths. That film (Gemini
      voices, sync-3 lips) is kept as gs://.../godavari10/final/voice_test/Godavari_Simham_geminiVoice_sync3lips_1080p.mp4.
      The redo was approved with the hard stop raised to $155. **Ledger: $151.57** (sync.so $52.88 in all, plus
      $0.08 for a new Lyria piece under the trailer, which changed length with the E lines; two
      sync.so credit top-ups by the owner were needed, the API answers 402 when credits run out and the run resumes
      without paying twice).
    - **QA (frame level, every shot):** frame strips of the Veo crop against the sync-3 result with the line's
      loudness, mouth aperture from the face mesh in speech and in silence, a block scan for changes outside the
      speaker's face (none over the codec noise in any shot), the speaker-lock review sheets (the right face in all
      34), and the film-level checks (14880 frames, every sampled cut on its frame, the voice 5 ms behind the
      lip-sync track in all 34 shots as before, -14.6 LUFS, true peak -0.2 dBTP). sync-3 articulates clearly better
      than LatentSync (teeth, mouth shapes on the words) and **closes the mouth in silence**, which fixes the old
      closed-mouth-during-speech shots (sc008_s002, sc009_s006, sc011_s004) and Bhairava's silent shouting tails.
      The one weak spot is the profile shot sc013_s009 (frames 71-101 barely move): shown as filmed there with
      fades (`filmed_frames`, composite only); `fade_frames` at the segment joins of sc009_s004 and sc013_s009.
      All the LatentSync-era frame settings were dropped (they were tuned to its output). Several E lines are shorter
      than the Gemini ones, so some shots show 1-3.5 s of the actor after the line; sync-3 keeps those mouths still.
  - **Known limits (LatentSync 1.6):** lips and teeth softer than filmed, Bhairava's gold teeth come out pale, some
    vowels under-articulated. In 3 shots the mouth stays closed for 0.8-1.7 s while the voice plays (sc008_s002,
    sc009_s006, sc011_s004). Only a new GPU pass (higher audio guidance, re-synced frame ranges; about $2) can change
    these; it needs the owner's OK. 18 Veo clips have letterbox bars baked in (12 of them 108-131 px); a uniform 2.35:1
    matte over the whole film would hide the change at cuts (owner's choice).
  - Still missing: sound effects (no SFX track yet; 77 distinct effects are spotted in projects/godavari10/post/
    sfx_spot_list.csv; candidate: Stable Audio 3.0 Small SFX, open weights, commercial use under $1M revenue).

### Verified vs. not verified

| Verified (here, with ffmpeg and a fake Google client) | Not verified yet |
|---|---|
| Every stage end to end, output files decode, loudness, subtitles, budget stop, safety-filter handling, caching, SDK signatures | InsightFace/SpeechBrain adapters, YouTube upload |
| Real Veo 3.1, Gemini pro image and Gemini TTS calls on Vertex AI (trailer test, 2026-10-13) | |
| Real Lyria 3 Pro music and LatentSync lip-sync on a Vertex L4 endpoint (2026-10-14) | |
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

## 2. Next steps after the final trailer (2026-10-14)

The project media is not in git. Restore it from `gs://nraiorbit-agentic-film-product-media/godavari5/backup/`
(or the chat copies) before any run; otherwise paid clips would be generated again. Setup and the copy-paste
prompt: [NEXT_SESSION_PROMPT.md](NEXT_SESSION_PROMPT.md).

| # | Step | Cost |
|---|---|---|
| 1 | You: new service account key (delete `71e11ffe…`), roles Vertex AI User + Storage Admin instead of Owner, env settings | — |
| 2 | Restore the backups; free trailer rebuild with `--max-spend 68.70` | free |
| 3 | **YouTube reference learning** (next feature): Gemini on Vertex watches public YouTube links you pass (Telugu action scenes, BGM) and writes reference profiles (character archetypes, emotional beats, BGM cue anatomy) that the story, casting and music agents use as examples. No downloading, no training on copyrighted films | ≈ $0.05–0.30 per video analysed |
| 4 | Full 5 minutes: `nraiorbit run godavari5 --yes --max-spend 200` (37 Veo shots + lip-sync of the new talking shots) | ≈ $121 + ≈ $4 GPU |
| 5 | Retakes of shots you pick | ≈ $3.33 each |
| 6 | Designed Telugu title image | ≈ $0.13 |
| 7 | Ask Google for Cloud Run GPU or Vertex training L4 quota (faster start, no 25-min endpoint deploy) | — |

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
| Lip-sync | Done (2026-10-14): LatentSync 1.6 on a Vertex L4 endpoint | Licence check of LatentSync weights and InsightFace before commercial use | Licence decision |
| Music | Done (2026-10-14): Lyria 3 Pro with your picked styles | YouTube reference learning to improve cues | Links to reference scenes |
| Automatic quality retakes | Today: one attempt per shot; you judge faces | Identity guard with a face model on each clip | Licence choice: InsightFace models are non-commercial (buy a licence and run it on the Google Cloud GPU), or AWS Rekognition (Google Cloud has no face-matching API) |
| Telugu quality | Dialogue was written by AI | Native review of every scene; recorded voices for leads | Your review, or a Telugu writer/voice artists with consent |
| DaVinci Resolve timeline | Today: movie + stems, not an editable timeline | OTIO/FCPXML export | — |
| Teaser, thumbnail | Not built | Teaser cutter (60 s), thumbnail from best keyframe | — |
| YouTube upload | Code exists, never run | Test upload as private | Google Cloud OAuth client + verified channel (needed for videos over 15 min) |
| New stories automatically | Story/screenplay agents need the Claude API | Use an Anthropic API key, or keep writing stories in Claude Code | API key from console.anthropic.com (optional) |
| Feature-length scale | 1h45m ≈ 1,400 shots, ≈ $4,500 per pass at Veo 3.1 standard 1080p (≈ $6,000–$9,000 with retakes) | Temporal workflows, review UI, music stem library, consented voice bank | Budget and timeline decision |
| Merge to main | Work is on a feature branch | Pull request | Say "open a PR" |
