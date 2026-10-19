# The always-on runner

A Cloud Run **job** that wakes every 15 minutes, reads two CSV queues in the private bucket, does one unit of work
per queue and exits. No GPU and no idle cost: a tick that finds nothing exits in seconds; a film costs 2 to 3
CPU-hours of Cloud Run over its life (under $1) plus the paid models, which the row's `budget_usd` caps.

| Queue | Columns | States |
|---|---|---|
| `queue/stories.csv` | slug, story_uri, budget_usd, gates (ask/auto), publish (none/private/unlisted/public), state, note, updated | new → awaiting:\<gate\> → (owner sets `approved`) → producing → done; blocked:\<reason\> → (owner sets `retry`) |
| `queue/references.csv` | url, title, learn (all/score/emotion/action/characters/trailer), state, profile_uri, note, updated | new → done / blocked:\<reason\> |

Setup: `PROJECT=… REGION=us-central1 BUCKET=… deploy/service/setup.sh` (image, service account, secrets, job,
schedule). Secrets to create first in Secret Manager: `ANTHROPIC_API_KEY` (the planning agents; an
organization-level key also needs the env var `ANTHROPIC_WORKSPACE_ID=wrkspc_…` on the job, a workspace-scoped key
needs nothing), `SYNC_API_KEY`, `ELEVENLABS_API_KEY`,
`GOOGLE_APPLICATION_CREDENTIALS_JSON` (a key for a service account that can sign download links) and, for
publishing, `YOUTUBE_TOKEN_JSON` (the OAuth token made once on a laptop with `nraiorbit release youtube <slug>
--auth`; without it a row's upload is reported as skipped and the film still lands in the bucket). The repository's
`.gcloudignore` keeps film projects, caches and media out of the image upload.

Deployed 2026-10-17 in `nraiorbit-agentic-film-product` (us-central1): the job `nraiorbit-runner` runs as
`nraiorbit-runner@…` (Vertex AI user, bucket objects, secret access, logs), the scheduler `nraiorbit-runner-tick`
fires it every 15 minutes, and the queues live at `gs://nraiorbit-agentic-film-product-media/queue/`. The four
model secrets and the YouTube token (stored by **Connect YouTube** in the control room) are in Secret Manager;
the job carries `ANTHROPIC_WORKSPACE_ID` for the organization-level key (2026-10-19).

Two things a tick guarantees on the bucket. It claims its row (`producing`, with the time) before it restores
anything, and every row it writes goes through a generation precondition on the sheet object, re-read and redone on
a conflict, so a save in the control room and a tick's claim seconds apart never lose each other's edit; a fresh
claim is left alone by other ticks for six hours (`NRAIORBIT_CLAIM_HOURS`). And the project's `reviews` folder, the
gate markers, is mirrored with deletes only by a tick that restored that folder in the same run, so a marker the
runner consumed never comes back to be approved twice, while a failed restore can never wipe an approval: a tick
whose restore of `reviews` or `reports` fails sets the row to `retry`, runs nothing, and the next tick tries again.

While it works, a tick keeps two small JSON records in the bucket for the control room (`service/progress.py`),
each written whole. `queue/running/<execution>.json` (Cloud Run's `CLOUD_RUN_EXECUTION`) says what the execution is
doing (library-restore, reference, story, idle, library-backup), on which film and phase; the tick removes it when
it exits and deletes any older than seven hours, left by a killed execution. `<slug>/reports/progress.json`, also in
the project's `reports/` so the backup carries it, is the film's state (running, then done, awaiting, blocked or
failed), its phase (a pipeline stage, or cost, faces, qa, deliver, publish, backup) and its last log line. A phase
goes up at once, log lines at most every 15 s, and a failed upload is logged once while the run goes on.

A queued film is made from the projects folder's `_template` when it has one, else from the image's
`/app/projects/_template` (the repository's): the runner's projects live on `/work/projects`, which has none.

Before anything is paid, `produce` prices the row's run and blocks without a cap or over it (docs/FRAMEWORK.md §7):
a row needs `budget_usd`. Watch a tick: `gcloud run jobs executions list --job nraiorbit-runner --region us-central1`
and its logs in Cloud Logging; the per-project report is `reports/produce.json` in the project's backup.

What the owner still decides, by design: approving the story, the cast and the faces when `gates` is `ask`, the cap
per film, and flipping a YouTube upload from private to public. Reference profiles are craft notes written by Gemini
from public links; the runner never downloads or copies footage, voices or music (see `docs/FRAMEWORK.md`).
