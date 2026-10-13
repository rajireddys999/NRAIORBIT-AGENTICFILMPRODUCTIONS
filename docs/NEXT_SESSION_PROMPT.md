# Prompt for the next session

## Where things stand (2026-10-13)

- The **trailer test is done** on Vertex AI: 17 Veo 3.1 clips at 1080p, 13 keyframes and 13 Telugu voice lines,
  giving `trailer.mp4` (32 s) and `movie.mp4` (5 min, with 37 placeholder shots). You approved the three faces and the 13 lines.
- Spend so far: **$57.65** on the ledger (photos $1.47 + trailer run $56.18). The real bill should be lower,
  because Veo audio is off. Check Google Cloud → Billing → Credits.
- Project media (paid clips, keyframes, voices, photos) is **not in git**. It was saved as a backup:
  - the 14 files `godavari5_backup.tar.part00` … `part13`, plus `godavari5_backup.sha256`, sent in the chat of the
    session that made the trailer, and
  - a Google Cloud Storage copy at `gs://nraiorbit-agentic-film-product-media/godavari5/`, once it has been made
    (needs `storage.googleapis.com` in the network allow-list).
- Restoring the backup means **nothing is paid again**. A restore test into a fresh clone rebuilt the trailer and
  movie with every Google call blocked, and the ledger stayed at $57.65.

## Before you start (once)

1. **Google Cloud → IAM → Service Accounts → `nraiorbit-agentic-film-product@…` → Keys:**
   - Delete the old key `71e11ffe…`, because it passed through chat.
   - Click **Add key → JSON** to download a new one.
   - In **IAM**, give the account the roles **Vertex AI User** (`roles/aiplatform.user`, which may show as "Agent
     Platform User") and **Storage Admin**, instead of **Owner**.
2. **Environment settings** (title bar → environment → Edit). Never paste keys into the chat.
   - `GOOGLE_APPLICATION_CREDENTIALS_JSON` = the whole text of the new JSON key file
   - `GOOGLE_CLOUD_PROJECT` = `nraiorbit-agentic-film-product`
   - `GOOGLE_CLOUD_LOCATION` = `us-central1`
3. **Network access → Allowed domains**, keeping "Allow package managers" ticked:
   `pypi.org`, `files.pythonhosted.org`, `oauth2.googleapis.com`, `aiplatform.googleapis.com`,
   `us-central1-aiplatform.googleapis.com`, `www.googleapis.com`, `storage.googleapis.com`.
4. Start a **new session** on this repository, branch `claude/telugu-ai-movie-generator-yhb6po`.
   If there is no Cloud Storage copy yet, attach the 14 backup parts and `godavari5_backup.sha256` to your first message.
5. Paste the prompt below.

## Prompt (copy everything in the box)

```text
Continue the NRAIORBIT Telugu film POC on branch claude/telugu-ai-movie-generator-yhb6po. Read README.md,
docs/PROJECT_STATUS.md, docs/POC_RUNBOOK.md and docs/NEXT_SESSION_PROMPT.md first. Talk to me in short, simple English.

State: the trailer test is done on Vertex AI (17 Veo clips, 13 Telugu lines, $57.65 on the ledger). I approved
the three faces and the 13 Telugu lines. Project media is not in git; restore it from the backup.
You may run commands that use GOOGLE_APPLICATION_CREDENTIALS_JSON / GOOGLE_CLOUD_PROJECT / GOOGLE_CLOUD_LOCATION.
Never print, log or commit any key. No paid step without my explicit OK in this session.

1. Check the three GOOGLE_* variables are set without printing them. Write the JSON to a 0600 file outside the
   repo only if a tool needs a file (gcloud), and delete it when done.
2. pip install -e ".[dev,google]", run the test suite. If a host is blocked, tell me which one and stop.
3. Restore projects/godavari5 and library/characters/*/refs:
   a) If gs://nraiorbit-agentic-film-product-media/godavari5/backup/ exists, copy the backup files from it;
   b) else use the godavari5_backup.tar.part* files I attached (in the uploads folder).
   Join them (cat godavari5_backup.tar.part* > godavari5_backup.tar), check with godavari5_backup.sha256,
   then tar -xf it in the repo root. If neither is available, STOP and tell me. Do not regenerate paid clips.
4. Free checks: `nraiorbit google models --vertex`; `nraiorbit estimate godavari5` ("Actual spend" should be
   $57.65). Rebuild the trailer for free with a cap just above what was spent, so no paid call is possible:
   `nraiorbit run godavari5 --only trailer --yes --max-spend 57.80`. Send me the trailer (compress to < 30 MB).
5. If the Cloud Storage copy does not exist yet: create a PRIVATE bucket gs://nraiorbit-agentic-film-product-media
   in asia-south1 (uniform access, public access prevention enforced). Upload the backup parts and the sha256 file
   to gs://nraiorbit-agentic-film-product-media/godavari5/backup/, and
   11_master/{trailer.mp4,movie.mp4,subs_te.srt,subs_en.srt,stems/} to .../godavari5/11_master/. Tell me what is stored and that it costs
   about $0.02/month.
6. STOP and ask me what to do next. Options (all need my OK, each with its cost):
   - Full 5 minutes: `nraiorbit run godavari5 --yes --max-spend 200` (37 more Veo shots, about $121 on the ledger).
   - Redo shots I name: `nraiorbit retake godavari5 <shot_id> --yes --max-spend <cap>` (about $3.33 each).
   - Music: Lyria on Vertex (lyria-002 / lyria-3-pro-preview are listed) needs a new provider, or my own tracks
     in inbox/music/.
   - Lip-sync on a Google Cloud L4 GPU (needs GPU quota).
   After any paid run, upload the new clips to the bucket.

Rules: projects/ and library media stay gitignored. If code must change, add tests, run the suite, then commit
and push to the same branch. Update docs/PROJECT_STATUS.md with what was done and what was spent.
```
