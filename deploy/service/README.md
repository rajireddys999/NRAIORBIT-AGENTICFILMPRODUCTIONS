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
publishing, the YouTube OAuth token made once on a laptop.

What the owner still decides, by design: approving the story, the cast and the faces when `gates` is `ask`, the cap
per film, and flipping a YouTube upload from private to public. Reference profiles are craft notes written by Gemini
from public links; the runner never downloads or copies footage, voices or music (see `docs/FRAMEWORK.md`).
