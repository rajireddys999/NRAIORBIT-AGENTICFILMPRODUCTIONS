# Prompt for the next session

## Where things stand (2026-10-14)

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
Continue the NRAIORBIT Telugu film POC on branch claude/telugu-ai-movie-generator-yhb6po. Read README.md,
docs/PROJECT_STATUS.md, docs/POC_RUNBOOK.md and docs/NEXT_SESSION_PROMPT.md first. Talk to me in short, simple English.

State: the final trailer (v3) is done on Vertex AI: 17 Veo clips, 13 Telugu lines (voice A), a Lyria 3 Pro score with
music.styles {elevation: epic_mythic, emotion: alaap_choir}, every talking shot lip-synced (LatentSync on a Vertex L4
endpoint), $68.61 on the ledger. I approved the faces, the 13 Telugu lines, the voice and the music styles.
Project media is not in git; restore it from gs://nraiorbit-agentic-film-product-media/godavari5/backup/.
You may run commands that use GOOGLE_APPLICATION_CREDENTIALS_JSON / GOOGLE_CLOUD_PROJECT / GOOGLE_CLOUD_LOCATION.
Never print, log or commit any key. No paid step without my explicit OK in this session.

1. Check the three GOOGLE_* variables are set without printing them. Write the JSON to a 0600 file outside the
   repo only if a tool needs a file (gcloud), and delete it when done. Run gcloud with
   `env -u CLOUDSDK_AUTH_ACCESS_TOKEN` (a preset token overrides the service account).
2. pip install -e ".[dev,google]", run the test suite. If a host is blocked, tell me which one and stop.
3. Restore projects/godavari5 and library/characters/*/refs from gs://nraiorbit-agentic-film-product-media/godavari5/backup/
   (or from backup parts I attach): for each of godavari5_backup, godavari5_update_v2, godavari5_update_v3 in that
   order, cat <name>.tar.part* > <name>.tar, check <name>.sha256, tar -xf in the repo root. If neither source is
   available, STOP and tell me. Do not regenerate paid clips, voices, music or lip-sync.
4. Free checks: `nraiorbit google models --vertex`; `nraiorbit estimate godavari5` ("Actual spend" should be
   $68.61). Rebuild the trailer for free with a cap just above what was spent, so no paid call is possible:
   `nraiorbit run godavari5 --only trailer --force render --yes --max-spend 68.70`. Send me the trailer (< 30 MB).
5. Next feature, YouTube reference learning: I will pass YouTube links of Telugu action scenes and background scores.
   Build it so Gemini on Vertex AI watches each public link (no downloading, no training on the films) and writes a
   structured reference profile (character archetypes and strengths, emotional beats, BGM cue anatomy: build, drop,
   hit, instruments, tempo, raga feel) into the asset library. The Story Analyst, Casting and Music Director agents
   then use the profiles as examples, and the Lyria prompts use the BGM profiles. Show me the cost per video before
   analysing any.
6. Then STOP and ask me what to do next. Options (all need my OK, each with its cost):
   - Full 5 minutes: `nraiorbit run godavari5 --yes --max-spend 200` (37 more Veo shots + keyframes, about $121 on
     the ledger, plus about $4 of GPU time to lip-sync the new talking shots; voices and music are already paid).
   - Redo shots I name: `nraiorbit retake godavari5 <shot_id> --yes --max-spend <cap>` (about $3.33 each).
   - Re-score one cue in another style: change music.styles (presets in config/music_styles.yaml), about $0.50 per piece.
   - A designed Telugu title image (about $0.13).
   After any paid run, make an update backup and upload it and the new master files to the bucket.

Rules: projects/ and library media stay gitignored. If code must change, add tests, run the suite, then commit
and push to the same branch. Update docs/PROJECT_STATUS.md with what was done and what was spent.
```
