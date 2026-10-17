# Prompt for the next session

## Where things stand (2026-10-17): the voice change

- The owner compared voices (docs/PROJECT_STATUS.md, "Voice test"): the whole film exists in versions **E** and **F**
  (ElevenLabs Eleven v4, native Telugu library voices) under gs://.../godavari10/final/voice_test/. **Not chosen yet.**
- The chosen voice needs the 34 on-camera speaking shots lip-synced again. Pre-flight done, nothing paid: the
  `SYNC_API_KEY` secret works, `sync-3` and `lipsync-2-pro` are valid model ids, the service account signs links.
  Exact cost (36 speaker crops, 198.8 s): **sync-3 $26.44 → ledger $125.05**, **lipsync-2-pro $16.50 → $115.11**.
  The ledger stands at $98.61 with a $115 hard stop, so the owner must say both the voice (E or F) and the new cap
  (for example "raise the hard stop to $150") before the run.
- The run, once approved (scripts in gs://.../godavari10/project/scripts/, written for the session scratchpad):
  `install_takes.py E|F` (the version's takes become 05_dialogue/<line>/take_1.wav; Gemini takes kept as
  take_1.gemini.wav), `configure_sync3.py apply` (providers.lipsync → sync_so, lipsync.model, offline off, the
  LatentSync frame-level settings dropped, `sync: false` / `sync_s` / `cut_in_s` kept; backup in
  pipeline.latentsync.yaml), then `nraiorbit run godavari10 --force render --yes --max-spend <new cap>` through
  genv.sh (the Google key). sync.so jobs run one at a time; a stopped run resumes without paying twice
  (cache/lipsync/<key>.pending.json). If the result download is refused by the network policy, the error names the
  host to allow. Then: frame-level QA of the 34 shots (new per-shot settings as needed), rebuild film and trailer,
  final deliverables, bucket backup, docs.

## Where things stand (2026-10-15): the 10-minute film

- **godavari10**, the whole Godavari Simham story as a 10-minute short (Plan A, owner-approved, $200 incl. GST):
  13 sequences, 133 shots, 58 Telugu dialogue blocks (all approved; AI voices), 20 Lyria cues, Telugu title /
  INTERVAL / end cards. 11 new adult characters approved (no children on screen). Every first frame was checked
  against the approved faces before video was paid (`nraiorbit keyframes`).
- Full cut with lip-sync done: **$97.17 on the godavari10 ledger** (hard stop $115). All 34 on-camera speaking shots
  are lip-synced (LatentSync on Vertex L4: one whole-frame batch, then speaker crops for 9 shots). Two frame-level QA
  rounds (docs/PROJECT_STATUS.md) were fixed for free: speaker lock, face-mesh paste-back, frame-grid cuts and
  per-shot settings in `projects/godavari10/pipeline.yaml` → `lipsync.shots` (26 shots). `lipsync.offline: true`
  there, so a rebuild never starts a GPU job. Each rebuild is sent in chat as 6 parts under 30 MB plus the trailer.
- Open choice for the owner: a targeted GPU re-run (about $2) for the closed mouths in sc008_s002, sc009_s006 and
  sc011_s004 and some under-articulated vowels. This needs a small code change (re-sync only given frame ranges with
  higher audio guidance) and the owner's OK. Also a uniform 2.35:1 matte (18 clips have baked-in bars), and three
  free edit/sound options the QA critic left to the owner (docs/PROJECT_STATUS.md, round 3).
- If a session finds a lip-sync endpoint still deployed with nothing running, undeploy and delete it
  (`gcloud ai endpoints list --region us-central1`).
- Media: `gs://nraiorbit-agentic-film-product-media/godavari10/` (inbox clips, keyframes, caches, plan files, cost
  log) and `gs://nraiorbit-agentic-film-product-media/library/characters/<id>/refs` for the 11 new characters.
  Arjun, Meera and Bhairava's photos are in the godavari5 backups below.
- Not done yet: sound effects (77 effects spotted in projects/godavari10/post/sfx_spot_list.csv), a native Telugu
  review of the lines, the InsightFace licence question before any monetised release.

## Earlier (2026-10-14): the 5-minute POC trailer

- **Final trailer done** (v3) on Vertex AI:
  - 17 Veo 3.1 clips at 1080p and 13 Telugu lines (voice A, gemini-2.5-flash-tts).
  - A Lyria 3 Pro score with `music.styles` set to `{elevation: epic_mythic, emotion: alaap_choir}`.
  - Every talking shot is lip-synced with LatentSync 1.6 on a Google Cloud L4 GPU (7 shots), and no talking face is silent.
  - A gold title card.
  - You approved the faces, the 13 lines, the voice and the music styles.
- **Spend so far: $68.61** on the ledger:

  | Item | Cost |
  |---|---|
  | Veo clips | $54.40 |
  | Images (photos + keyframes) | $3.22 |
  | Voices | $0.07 |
  | Lyria music, including auditions | $8.00 |
  | Lip-sync GPU | $2.93 |

  The Lyria and GPU prices are cautious placeholders, and Veo is counted with audio even though audio is off, so the
  real bill should be lower. Check it in Google Cloud → Billing → Credits.
- **Lip-sync runs on a Vertex AI endpoint** (`lipsync.runner: vertex_endpoint`) because the only GPU quota is Vertex
  *serving* L4 = 2 in us-central1. Cloud Run GPU and Vertex training L4 quotas are 0.
  - Setup takes about 25 min, then about 8–10 min per 1080p shot.
  - The endpoint is always deleted after the batch.
  - The worker image `us-central1-docker.pkg.dev/nraiorbit-agentic-film-product/nraiorbit/latentsync:v2` is already built.
- Project media is **not in git**. It is stored in the private bucket `gs://nraiorbit-agentic-film-product-media`
  (us-central1, public access blocked):
  - `godavari5/backup/`: the base backup (`godavari5_backup.tar.part00`…`part13`), update v2
    (`godavari5_update_v2.tar.part00`…`part02`) and update v3 (`godavari5_update_v3.tar.part00`…`part04`), each with
    its `.sha256` file. The same files were also sent in chat.
  - `godavari5/11_master/`: the original trailer, the 5-minute movie, the subtitles and the stems.
- Restoring the three backups in order (base, then v2, then v3) means **nothing is paid again**: every clip, voice,
  music piece and lip-sync result is cached.

## Before you start (once)

1. **Google Cloud → IAM → Service Accounts → `nraiorbit-agentic-film-product@…` → Keys:**
   - Delete the old key `71e11ffe…`, because it passed through chat.
   - Click **Add key → JSON** to download a new one.
   - In **IAM**, give the account the roles **Vertex AI User** (`roles/aiplatform.user`) and **Storage Admin**
     instead of **Owner**. Lip-sync also needs **Service Account User** on itself, and Cloud Build needs
     **Cloud Build Editor**, but only if the worker image must be rebuilt.
2. **Environment settings** (title bar → environment → Edit). Never paste keys into the chat.
   - `GOOGLE_APPLICATION_CREDENTIALS_JSON` = the whole text of the new JSON key file
   - `GOOGLE_CLOUD_PROJECT` = `nraiorbit-agentic-film-product`
   - `GOOGLE_CLOUD_LOCATION` = `us-central1`
3. **Network access → Allowed domains**, keeping "Allow package managers" ticked:
   `pypi.org`, `files.pythonhosted.org`, `oauth2.googleapis.com`, `aiplatform.googleapis.com`,
   `us-central1-aiplatform.googleapis.com`, `www.googleapis.com`, `storage.googleapis.com`.
4. Start a **new session** on this repository, branch `claude/telugu-ai-movie-generator-yhb6po`, and paste the prompt below.

## Prompt (copy everything in the box)

```text
Continue the NRAIORBIT Telugu film work on branch claude/telugu-ai-movie-generator-yhb6po. Read README.md,
docs/PROJECT_STATUS.md and docs/NEXT_SESSION_PROMPT.md first. Talk to me in short, simple English.

State: the 10-minute short film "godavari10" (Plan A) is cut, lip-synced and QA'd twice ($97.17 of the $115 hard
stop). I approved every Telugu line, the 11 new characters and the video-model mix.
Project media is not in git; restore it from the private bucket gs://nraiorbit-agentic-film-product-media.
You may run commands that use GOOGLE_APPLICATION_CREDENTIALS_JSON / GOOGLE_CLOUD_PROJECT / GOOGLE_CLOUD_LOCATION.
Never print, log or commit any key. No paid step without my explicit OK in this session.

1. Check the three GOOGLE_* variables are set without printing them. Run gcloud with
   `env -u CLOUDSDK_AUTH_ACCESS_TOKEN` (a preset token overrides the service account). First check
   `gcloud ai endpoints list --region us-central1`: if a lip-sync endpoint is still deployed, tell me, then undeploy
   and delete it.
2. pip install -e ".[dev,google,lipsync]", run the test suite. If a host is blocked, tell me which one and stop.
   Install the Telugu card font: Noto Serif/Sans Telugu Bold into /usr/local/share/fonts/noto-telugu/ (from
   fonts.gstatic.com via fonts.googleapis.com/css2?family=Noto+Serif+Telugu:wght@700).
   The lip-sync speaker lock (MediaPipe) needs libEGL.so.1 and libGLESv2.so.2. apt over plain HTTP is blocked by
   the proxy, so fetch the two packages over HTTPS and install them:
   https://archive.ubuntu.com/ubuntu/pool/main/libg/libglvnd/libegl1_1.7.0-1build1_amd64.deb and
   .../libgles2_1.7.0-1build1_amd64.deb, then `dpkg -i` both. A "dependency problems" message is fine: the
   libraries are unpacked. The face detector (1 MB) and face mesh (4 MB) models download once from
   storage.googleapis.com on the first lip-sync run.
3. Restore, never regenerate paid work:
   a) godavari5 backups (Arjun, Meera, Bhairava's photos live there): from gs://.../godavari5/backup/, for each of
      godavari5_backup, godavari5_update_v2, godavari5_update_v3 in order: cat parts > .tar, check .sha256, tar -xf.
   b) gcloud storage rsync -r gs://.../godavari10/inbox projects/godavari10/inbox, the same for 06_keyframes, cache,
      08_music and reports; copy gs://.../godavari10/project/* into projects/godavari10/ (03_scenes, 04_shots,
      pipeline.yaml, pricing.yaml, cast.lock.yaml, takes.yaml, reuse.yaml, 01_story_bible.json, 02_outline.json, post/).
   c) gcloud storage rsync -r gs://.../library/characters library/characters (new characters' photos).
4. Free rebuild with a cap just above the ledger so nothing can be paid (lipsync.offline is true in the project's
   pipeline.yaml, so no GPU job starts either):
   `nraiorbit run godavari10 --force render --yes --max-spend <ledger total + 0.10>`. Check reports/lipsync.jsonl:
   every synced shot must say "speaker crops" or "whole frame", none "not synced". Check the cut (one frame per shot,
   no stray text, faces match), then send me the film in parts under 30 MB.
5. Then STOP and ask me what to do next. Options (each needs my OK, with its cost):
   - Targeted lip-sync re-run (about $2 of GPU, plus code to re-sync only given frame ranges with higher audio
     guidance) for the closed mouths in sc008_s002, sc009_s006, sc011_s004 and the vowels listed in PROJECT_STATUS.
   - Sound effects for fights, rain, fire and crowds (open model on our L4, about $1-3 of GPU, plus code).
   - Redo shots I name: `nraiorbit retake godavari10 <shot_id> [--keep-keyframe] --yes --max-spend <cap>`
     (Fast 720p about $0.32-0.48 per shot, plus $0.15 for a new first frame).
   - YouTube reference learning (Gemini watches public links I give and writes reference profiles for the agents).

Rules: projects/ and library media stay gitignored. If code must change, add tests, run the suite, then commit
and push to the same branch. Update docs/PROJECT_STATUS.md with what was done and what was spent.
```
