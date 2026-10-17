# The control room

One private web page over the bucket and the two queue sheets: a story form, a board of films with their state, gates,
spend and estimate, a page per film with every stage's output (story bible, outline, scenes and dialogue, cast, shot
lists, first frames, render progress, QA, the film and trailer, delivery links, the YouTube result, the run report
and the runner's log), approve / retry / cap buttons, and the reference queue.

It writes only `queue/stories.csv`, `queue/references.csv` and `stories/<slug>.*`; the runner does every paid step
under the row's cap. There is no database: the page can never disagree with what the runner sees.

- Locally: `nraiorbit ui --root projects --no-auth` (reads the projects folder; no sign-in, so never on the internet).
- On Cloud Run: `PROJECT=… BUCKET=… ALLOW=you@gmail.com deploy/ui/setup.sh`. The service scales to zero and sits
  behind Identity-Aware Proxy: Google signs the visitor in and lets only the addresses in ALLOW through; the app
  checks the same address again (`UI_ALLOWED_EMAILS`) as a second lock. It runs as the runner's service account with
  the runner's key secret, so signed links, bucket reads and "run a tick now" work the same way they do in the job.

Deployed 2026-10-17: service `nraiorbit-ui` in `nraiorbit-agentic-film-product` (us-central1), allow-list
rrshiva361@gmail.com. The link is in docs/PROJECT_STATUS.md.
