# The always-on runner

A Cloud Run **job** that wakes every 15 minutes, reads two CSV queues in the private bucket, does one unit of work
per queue and exits. No GPU and no idle cost: a tick that finds nothing exits in seconds; a film costs 2 to 3
CPU-hours of Cloud Run over its life (under $1) plus the paid models, which the row's `budget_usd` caps.

| Queue | Columns | States |
|---|---|---|
| `queue/stories.csv` | slug, story_uri, budget_usd, gates (ask/auto), publish (none/private/unlisted/public), state, note, updated | new → awaiting:\<gate\> → (owner sets `approved`) → producing → done; blocked:\<reason\> → (owner sets `retry`) |
| `queue/references.csv` | url, title, learn (all/score/emotion/action/characters/trailer), state, profile_uri, note, updated | new → done / blocked:\<reason\> |

Setup: `PROJECT=… REGION=us-central1 BUCKET=… deploy/service/setup.sh` (image, service account, secrets, job,
schedule). Secrets to create first in Secret Manager: `SYNC_API_KEY`, `ELEVENLABS_API_KEY`,
`GOOGLE_APPLICATION_CREDENTIALS_JSON` (a key for a service account that can sign download links) and, for
publishing, `YOUTUBE_TOKEN_JSON` (the OAuth token made once on a laptop with `nraiorbit release youtube <slug>
--auth`; without it a row's upload is reported as skipped and the film still lands in the bucket). The repository's
`.gcloudignore` keeps film projects, caches and media out of the image upload.

Deployed 2026-10-17 in `nraiorbit-agentic-film-product` (us-central1): the job `nraiorbit-runner` runs as
`nraiorbit-runner@…` (Vertex AI user, bucket objects, secret access, logs), the scheduler `nraiorbit-runner-tick`
fires it every 15 minutes, and the queues live at `gs://nraiorbit-agentic-film-product-media/queue/`. The three
model secrets are in Secret Manager; the YouTube token is not yet (publishing waits for it).

Before anything is paid, `produce` prices the row's run and blocks without a cap or over it (docs/FRAMEWORK.md §7):
a row needs `budget_usd`. Watch a tick: `gcloud run jobs executions list --job nraiorbit-runner --region us-central1`
and its logs in Cloud Logging; the per-project report is `reports/produce.json` in the project's backup.

What the owner still decides, by design: approving the story, the cast and the faces when `gates` is `ask`, the cap
per film, and flipping a YouTube upload from private to public. Reference profiles are craft notes written by Gemini
from public links; the runner never downloads or copies footage, voices or music (see `docs/FRAMEWORK.md`).
