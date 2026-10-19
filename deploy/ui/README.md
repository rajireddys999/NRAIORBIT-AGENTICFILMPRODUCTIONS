# The control room

One private web page over the bucket and the two queue sheets: a story form, a board of films with their state, gates,
spend and estimate, a page per film with every stage's output (story bible, outline, scenes and dialogue, cast, shot
lists, first frames, render progress, QA, the film and trailer, delivery links, the YouTube result, the run report
and the runner's log), approve / retry / cap buttons, and the reference queue.

It writes only `queue/stories.csv`, `queue/references.csv` and `stories/<slug>.*`; the runner does every paid step
under the row's cap. There is no database: the page can never disagree with what the runner sees.

- Locally: `nraiorbit ui --root projects --no-auth` (reads the projects folder; no sign-in, so never on the internet).
- On Cloud Run: `PROJECT=… BUCKET=… ALLOW=you@gmail.com CLIENT_ID=… deploy/ui/setup.sh`. The service scales to zero.
  Sign-in is Google's own "Sign in with Google" button inside the page: the visitor's Google ID token is verified
  against the OAuth web client id in `UI_GOOGLE_CLIENT_ID`, the address must be in `UI_ALLOWED_EMAILS`, and a signed
  session cookie (`UI_SESSION_SECRET`, a Secret Manager secret) keeps them in for a week. Nothing else is ever
  trusted; without a session every page redirects to `/login` and every button answers 403. The client id is public
  and needs no secret: make it once under Google Auth Platform → Clients → Web application, with the service URL as
  an authorized JavaScript origin. A second way in, for the days before that client exists or for a phone without
  the account: a long passphrase in the Secret Manager secret `UI_PASSPHRASE` (`--update-secrets
  UI_PASSPHRASE=UI_PASSPHRASE:latest` on the service); wrong tries are throttled per address, and removing the secret
  from the service removes the form. (Identity-Aware Proxy also works in front, and the app accepts its signed
  assertion too, but IAP needs a custom OAuth client that only the console can make in a project without an
  organization, so the in-page sign-in is the default.) It runs as the runner's service account with the runner's
  key secret, so signed links, bucket reads and "run a tick now" work the same way they do in the job.

**A project without an organization** (any project owned by a plain Google account) has no Google-managed OAuth
client, so the first visit answers `Empty Google Account OAuth client ID(s)/secret(s)` until a custom client exists.
That is a one-time console step (the API routes for it were shut down in March 2026): configure the OAuth consent
screen under Google Auth Platform (External; add the allowed addresses as test users or publish the app), then
Security → Identity-Aware Proxy → Applications → the `nraiorbit-ui` row → settings → Custom OAuth → auto-generate
credentials → save. The script's IAP bindings stay valid; nothing else changes.

Deployed 2026-10-17: service `nraiorbit-ui` in `nraiorbit-agentic-film-product` (us-central1), allow-list
nraiorbit.cinematic.universe@gmail.com. The link is in docs/PROJECT_STATUS.md.

**Connect YouTube.** The board's YouTube card starts Google's authorization for the channel's account with the same
OAuth web client (its secret goes on the service as `UI_GOOGLE_CLIENT_SECRET`; the client needs
`<service url>/youtube/callback` among its authorized redirect URIs). The refresh token is stored as a new version
of the Secret Manager secret `YOUTUBE_TOKEN_JSON` (the service account holds `secretVersionAdder` on that secret
only), the channel's name goes to `settings/youtube.json` in the bucket, and the runner reads the secret when it
publishes, so no token file ever travels. Publish the consent screen (Audience → In production) before connecting:
a project left in Testing gets refresh tokens that die after seven days.

