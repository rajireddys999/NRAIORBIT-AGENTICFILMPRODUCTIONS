# Instagram before the YouTube release

Status: design, researched and reviewed on 2026-10-20. **Not built yet.** Nothing here runs today; the owner's setup
steps (section 2) can be done ahead of the build.

Scope: one film's Instagram campaign, from seven days before the YouTube release to one day after it.
The owner approves the whole campaign once, in the control room.
The runner then posts each item at its time.
The film goes public on YouTube at T by YouTube's own schedule, not by the runner.

How sure the Meta facts are: no Meta page could be opened live during the research.
The facts come from search extracts, Meta's own Postman collection, Meta's Python Business SDK, and copies of Meta's pages saved on 2025-12-14.
Step 0 of the build plan re-checks them.
Two one-time tests on the owner's real accounts (section 2, steps 12 and 13) settle what the documents cannot.

---

## 1. The process by hand

### The clock

- T is the moment the film goes public on YouTube.
- Every time in this section is IST (UTC+05:30), the audience's clock.
- The default T is a Tuesday, Wednesday or Thursday at 19:00 IST.
- That default rests on blog consensus only. The account's own Insights should replace it after a few films.
- The control room shows every time twice: in IST, and on the studio's clock (America/New_York).
- 19:00 IST is 09:30 in New York until 1 November 2026. From that day it is 08:30, because New York leaves daylight time.
- T should fall on a quarter hour, so that it lines up with the runner's 15-minute ticks.

### Rules for every item

- Every item carries the AI disclosure in three ways.
  - Meta's AI label, switched on when the item is posted.
  - A visible tag on the picture: "AI-made film · AI తో రూపొందించిన చిత్రం".
  - The same line in the caption, where the item has a caption.
- Stories take no caption, so the tag on the picture is their written disclosure.
- No date is burned into a feed post or a Reel. Those stay true if the release moves.
- The date goes only in captions and in stories. A caption can be edited by hand in the app. A story disappears after 24 hours.
- At most one feed post or Reel a day, and one to three stories a day. Meta's spam policy penalises very high frequency and repetitive content.
- At most five hashtags per post. Instagram has capped posts at five since December 2025 (secondary reports).
- Only the film's own sound is used. No Instagram library music.
- The three Reels use different footage. The teaser and the "out now" clip are cut from the film. The third Reel is the trailer.

### Before T−7, for this film

1. Choose T at least 8 days ahead. The extra day leaves time to approve the plan.
2. In YouTube Studio, upload the film as Private and schedule it for T.
3. Upload the trailer the same way, scheduled for T−3 at 18:45 IST.
4. Copy both links. Until T, the film's link shows as private to everyone except you. No pre-release item may point at it.
5. Make the assets:
   - a 4:5 first-look poster (1080x1350 JPEG), with the title and the AI tag, and no date;
   - a 9:16 version of it for the story, with the date;
   - two to four character cards (4:5);
   - a teaser of 15 to 30 seconds, cut from the film, vertical (1080x1920);
   - the trailer, cut vertical, with an end card;
   - an "out now" clip of 10 to 20 seconds, from another moment of the film;
   - story images: "New teaser on our profile", "2 days to go", "Tomorrow, 7 PM IST", "Tonight, 7 PM IST", "Watch now", "Now on YouTube".
6. Write each caption in this order: a Telugu line, an English line, the AI line, "Link in bio · YouTube", then three to five specific hashtags.
   Examples: #TeluguShortFilm #Tollywood #TeluguCinema, plus the film's title tag.
7. Set the profile's link once, to the YouTube channel. In the app: Edit profile → Links → Add external link.
   The channel link always works. It shows the trailer once that is public, and the film after T.

### The campaign

The example column uses T = Wednesday 4 November 2026, 19:00 IST. New York changes its clock on 1 November, in the middle of this campaign.

For every Instagram row, switch on Meta's AI label in the app when posting.

| When (IST, from T) | Example (IST · New York) | Surface | Item | By hand in the app |
|---|---|---|---|---|
| T−7, 19:00 | Wed 28 Oct 19:00 · 09:30 EDT | Feed post (image, 4:5) | First-look poster; the caption gives the date | — |
| T−7, 19:30 | Wed 28 Oct 19:30 · 10:00 EDT | Story (image, 9:16) | Poster with "4 Nov · 7 PM IST · YouTube" | — |
| T−6, 19:00 | Thu 29 Oct 19:00 · 09:30 EDT | Feed carousel (2–4 images, 4:5) | Character cards | — |
| T−5, 19:00 | Fri 30 Oct 19:00 · 09:30 EDT | Reel, also in the feed | Teaser, 15–30 s, cut from the film | Pick the cover |
| T−5, 19:30 | Fri 30 Oct 19:30 · 10:00 EDT | Story (image) | "New teaser on our profile" | — |
| T−3, 18:45 | Sun 1 Nov 18:45 · 08:15 EST | YouTube | The trailer goes public (scheduled) | — |
| T−3, 19:00 | Sun 1 Nov 19:00 · 08:30 EST | Reel, also in the feed | Vertical trailer | Check that the trailer is public first |
| T−3, 19:30 | Sun 1 Nov 19:30 · 09:00 EST | Story (video, at most 60 s) | Vertical trailer | Optional: a link sticker to the YouTube trailer |
| T−2, 19:00 | Mon 2 Nov 19:00 · 08:30 EST | Story (image) | "2 days to go" | Optional: a countdown sticker set to T |
| T−1, 19:00 | Tue 3 Nov 19:00 · 08:30 EST | Story (image) | "Tomorrow, 7 PM IST" | — |
| T, 10:00 | Wed 4 Nov 10:00 · Tue 3 Nov 23:30 EST | Story (image) | "Tonight, 7 PM IST" | — |
| T, 19:00 | Wed 4 Nov 19:00 · 08:30 EST | YouTube | The film goes public (scheduled) | — |
| T, 19:15 | Wed 4 Nov 19:15 · 08:45 EST | Reel, also in the feed | "Out now" clip from the film | Check that the film is public first |
| T, 19:15 | Wed 4 Nov 19:15 · 08:45 EST | Story (image) | "Watch now · link in bio" | Optional: a link sticker to the film; optional: point the profile link at the film |
| T+1, 19:00 | Thu 5 Nov 19:00 · 08:30 EST | Story (image) | "Now on YouTube · link in bio" | Reply to comments, pin up to 3, reshare reactions |

- That is five feed posts or Reels over eight days, never two on one day.
- Stories never pass two a day.
- After the release, read Insights: stories within 24 hours, because their data goes with them, and Reels after about two days.

---

## 2. One-time setup only the owner can do

1. **Make the Instagram account a Business account.**
   - In the Instagram app: your profile → the menu (three lines) → Settings and activity → Account type and tools → Switch to professional account → Business.
   - If it is already a Creator account, the same menu offers "Switch to business account".
   - Why Business: Meta documents Stories publishing as Business-only. It says nothing either way about Creator accounts on the Instagram Login path.
   - The account must be public. Meta's app setup page says an account added to the app must be public.

2. **Register as a Meta developer.** Open developers.facebook.com, press Get started, sign in with your Facebook account, and confirm your phone or e-mail.

3. **Create the app.** Open developers.facebook.com/apps and press Create app.
   - App name: any name without "Instagram", "IG", "Insta" or "gram". Instagram's brand rules forbid those. Example: "NRAIORBIT Studio Publisher".
   - Contact e-mail: yours.
   - Use case: "Manage messaging & content on Instagram".
   - Business portfolio: skip it. It is optional for an account you own.
   - Press Go to dashboard.
   - If that use case is missing, or the Instagram panel opens blank (one report from October 2026 describes this), use the older path: Create app → Other → Business → on the dashboard, Instagram → Set up.

4. **Set up "API setup with Instagram login".** Dashboard → Use cases → "Manage messaging & content on Instagram" → Customize → API setup with Instagram login.
   - In its permissions list, add `instagram_business_content_publish`. The setup starts with only `instagram_business_basic` and `instagram_business_manage_messages`, and cannot post without it.
   - `instagram_business_manage_messages` is not used by this design.
   - Open "3. Set up Instagram business login" → Business login settings.
   - Copy the Instagram app ID and the Instagram app secret. These are not the Meta app ID and secret under App settings → Basic.

5. **Make the Instagram account a tester of the app.**
   - App roles → Roles → Add People → choose "Instagram Tester" (not the plain "Tester") → type the Instagram username.
   - In a desktop browser signed in to that Instagram account, open instagram.com/accounts/manage_access/ (Settings → Website permissions → Apps and websites → Tester invites) and press Accept.
   - No e-mail is sent. Without this step, Connect fails with "Insufficient developer role".

6. **Add the redirect address.** In Business login settings → OAuth redirect URIs, add `https://<control-room-host>/instagram/callback`.
   - The host is the control room's address: `UI_PUBLIC_URL` when it is set, otherwise the Cloud Run URL.
   - Press Save. Check whether the dashboard added a trailing slash. The address must match the control room's exactly.
   - Leave the deauthorize and data-deletion fields empty. They matter only if the app ever goes Live.
   - Leave the app in Development mode.
   - Meta's App Review page says an app used only for a business you own or manage, with Instagram Login and Standard Access, needs no App Review.

7. **Create two secrets.** Open console.cloud.google.com, choose the project `nraiorbit-agentic-film-product`, and press Activate Cloud Shell. Then run:

   ```
   read -rs V && printf %s "$V" | gcloud secrets create INSTAGRAM_APP_SECRET --replication-policy=automatic --data-file=-; unset V
   gcloud secrets create INSTAGRAM_TOKEN_JSON --replication-policy=automatic
   SA=nraiorbit-runner@nraiorbit-agentic-film-product.iam.gserviceaccount.com
   for r in roles/secretmanager.secretVersionAdder roles/secretmanager.secretVersionManager; do
     gcloud secrets add-iam-policy-binding INSTAGRAM_TOKEN_JSON --member="serviceAccount:$SA" --role="$r"
   done
   ```

   - After the first line, paste the Instagram app secret and press Enter. `read -rs` hides it and keeps it out of the shell history.
   - The token secret starts empty. Connect adds its first version. A version cannot be added to a secret that does not exist.
   - The control room and the runner run as the same service account. The two roles let them add a token version and destroy old versions, on this one secret only.
   - Reading secrets already works through the account's project-wide `secretAccessor` role.

8. **Give the control room the app's ID and secret.**

   ```
   gcloud run services update nraiorbit-ui --region us-central1 \
     --update-env-vars INSTAGRAM_APP_ID=<the Instagram app ID> \
     --update-secrets INSTAGRAM_APP_SECRET=INSTAGRAM_APP_SECRET:latest
   ```

   - Do not re-run `deploy/ui/setup.sh` for this. Its `--set-secrets` replaces every secret on the service, so it would drop `UI_GOOGLE_CLIENT_SECRET` and break Connect YouTube.
   - The runner needs no new setting.

9. **Deploy the new code** (after it is merged). From the root of a checkout of the merged commit, where the setup scripts are run today, build both images, then point the job and the service at them.

   ```
   P=nraiorbit-agentic-film-product; R=us-central1; REPO=$R-docker.pkg.dev/$P/nraiorbit
   gcloud builds submit --project $P --config deploy/service/cloudbuild.yaml --substitutions _IMAGE=$REPO/runner:latest .
   gcloud builds submit --project $P --config deploy/ui/cloudbuild.yaml --substitutions _IMAGE=$REPO/ui:latest .
   gcloud run jobs update nraiorbit-runner --project $P --region $R --image $REPO/runner:latest
   gcloud run services update nraiorbit-ui --project $P --region $R --image $REPO/ui:latest
   ```

   - Do not re-run the setup scripts. They replace the environment variables and secrets, and the runner's `ANTHROPIC_WORKSPACE_ID` would be lost unless it is set again.
   - The same commit matters. The queue sheet gains a column, and an old image that rewrites the sheet would drop it.

10. **Keep the YouTube token alive.** In Google Auth Platform → Audience, the publishing status must read "In production".
    - In Testing, Google's refresh tokens die after seven days (deploy/ui/README.md).
    - The runner uses that token to check that the film really went public.

11. **Connect Instagram.** Control room → home → Instagram card → Connect Instagram → sign in to the Instagram account → Allow.
    - The card then shows the @handle, the account type (it should say BUSINESS) and the token's expiry date.

12. **Test Instagram.** Press Test Instagram on the card.
    - The control room starts a runner tick at once; if a tick is already running, the next scheduled one takes the request. The test runs there, and its result appears on the card a minute or two later.
    - The test reads the account's publishing limit.
    - It creates an image story container and a video story container from 24-hour signed links, with `is_ai_generated=true`, and waits until both are ready. It does not publish them.
    - It creates a Reel container the same way, and does not publish it.
    - Then press **Post the test story**, once. It publishes the image story, a plain studio card with the AI tag.
    - It reads back the story's `is_ai_generated` value and its type.
    - It then calls publish a second time on the same container. That call must fail. This proves a container cannot post twice.
    - The test story is public for 24 hours. Delete it in the app right away: open the story → ⋯ → Delete.
    - The card then shows "test passed" and the date. The runner posts nothing for any film until this test has passed.

13. **Test YouTube scheduling.** Press Test YouTube scheduling on the YouTube card.
    - The runner uploads a 5-second private test clip, with `publishAt` set 30 days ahead, and reads back its status.
    - It switches the clip to unlisted and reads back again.
    - It then deletes the clip. Nothing is ever public.
    - If the clip stays private, uploads from this Google Cloud project are locked. Apply for the YouTube API Services audit, and keep campaigns off until it passes.
    - The campaign's approve button stays disabled until this test has passed once.

14. **Optional: a failure e-mail.** Console → Logging → Logs Explorer → run the query `resource.type="cloud_run_job" AND textPayload:"PROMO_ALERT"` → Actions → Create log alert → choose your e-mail as the notification channel.
    - Cloud Monitoring may charge for alert policies. Check the price it shows before saving.
    - Without the alert, failures show only on the control room's board.

15. **For each film.** On the New story form, or on the film's row, set YouTube to `public` and Instagram to `campaign`.
    - When the film reaches the Instagram gate, read the plan and approve it.

---

## 3. How the runner automates it

### 3.1 Where it fits in `produce()`

Today the order is: plan → cost → faces → render, assemble, trailer → QA → deliver → publish → backup.

With Instagram on, it becomes: … → deliver → **promote** (draft the campaign, hold the gate) → **publish** (YouTube, private with a `publishAt` time) → **activate** (write the ledger) → backup → done.

The setting:
- The queue sheet gets a new owner column, `promote`, with the values `none` or `instagram`. The control room labels it "Instagram: none / campaign".
- `apply_row` writes it into `policy.promote` on every tick, as it does for `publish`.
- `policy_of` defaults it to `none`.
- With `promote: none`, `produce()` runs exactly as today. It even keeps the same two-argument call `publish_youtube(root, privacy)`.
- With `promote: instagram` and `publish` not `public`, the step records "promote: needs YouTube public", and the old publish flow runs unchanged.

The promote step:
1. **Guard against a second release.** If a live campaign exists for this film, but for a different cut, the step holds the gate with "A campaign is live for the earlier cut. Press Hold release first." Nothing is uploaded.
   - The cut is identified by the film's size and hash, the fingerprint `produce.py` already uses.
   - Without this guard, a re-cut would upload a second film scheduled for the same T.
   - Once the owner has pressed Hold release on that campaign, the old upload is unscheduled, and the new cut may go through.
2. **Reuse what was drafted.** If `12_promote/campaign.json` exists and its inputs are unchanged, the step reuses it. The inputs are the film and trailer fingerprints, the story bible, the outline, the cast lock and the owner's latest request. There is no new Claude call and no re-render.
3. **Otherwise, draft.**
   - Release time: the owner's request, else the first Tuesday, Wednesday or Thursday at `promote.release_time` (19:00 IST), at least `lead_days + 1` days away (8 by default).
   - Slots: laid out in code from the template in 3.4.
   - Items already posted in an earlier ledger for this film carry over as posted. They are not planned again.
   - Words: one call to a new `PromoterAgent` (3.3).
   - The owner's caption edits and dropped items are applied.
   - The assets are rendered into `12_promote/`.
   - `campaign.json` is written with a hash (`sha`) over the times, captions and asset checksums.
4. **Hold the gate.** The step holds `promote` with the gate text plus one JSON line, `{"release_at": ..., "campaign_sha": ...}`.
   - `approve_gate` already copies a pending marker's text into the `.approved` file, as the cost gate keeps its estimate.
5. The tick's backup then copies `12_promote/` to the bucket, as a new backup set. The control room shows the plan from there.

On the run after approval:
- The step compares the approved `sha` with the `sha` in `campaign.json`. It also checks each asset file's checksum against the one recorded.
- Any difference holds the gate again, with "the plan changed since you approved it".
- If T is less than 24 hours away, the step re-drafts with the next release day and holds again.
- Otherwise the step writes `publish.approved`. The one approval covers the scheduled upload, and the button says so.

Publish (details in 3.8):
- `publish_youtube(root, "public", publish_at=T, trailer_publish_at=…, after_record=…)` uploads both videos as private, with their `publishAt` times.
- It reads the film's status back. Private with `publishAt` equal to T is the only pass.
- Any failure finishes the run as `blocked:publish`. It is not reported as "skipped" with the run done. No campaign starts, and the approval stays for the retry.

Activate:
- The step creates `gs://<bucket>/<slug>/promote/ledger.json`, or merges into the existing one and keeps its posted items.
- It adds the film to `gs://<bucket>/queue/promotions.json`.
- It reports, for example, "campaign live: 13 items, first Wed 28 Oct 19:00 IST (09:30 EDT)".
- Then backup, then done. The row stays `done`. The 15-minute sweep drives the campaign from the bucket.

### 3.2 The campaign gate

Before the button, the film page's Instagram section shows:
- T in IST and New York time.
- The YouTube schedule: the trailer public at T−3, 18:45 IST; the film public at T.
- Every item: its time on both clocks, its surface, a preview of the asset, the full caption, and what it waits for (the trailer or the film being public).
- Items whose time has already passed, marked "will be skipped".
- The state of the connections: Instagram connected and tested, the YouTube test passed.
- A short checklist: the captions were read in Telugu and in English; no character looks like a real actor; the AI tag is visible on every asset.

The buttons:
- **Approve campaign and schedule YouTube.** It stays disabled until the YouTube test has passed once.
- **Edit captions / drop items.** A form per item. Saving writes `gs://<bucket>/<slug>/promote/request.json` and sets the row to `retry`. The runner applies the edits without a Claude call, and holds the gate again.
- **Move release.** An IST date and time, echoed in New York time as it is typed. It takes the same path.

Rules:
- The promote gate always asks, even under `gates: auto`. No setting skips it.
- If Instagram is not connected or not tested, the owner may still approve. YouTube is then scheduled, and the items wait for Instagram. An item whose day passes first is skipped.

### 3.3 Assets

Everything is made from what the run already has, with local ffmpeg and Pillow. Nothing is paid.

The words come from one structured Claude call (`PromoterAgent`). It reads the story bible, the outline's scene synopses, the cast, and the Telugu and English subtitles with their times. It returns, per item, a Telugu caption, an English caption, up to five hashtags and alt text for images. It also picks the poster frame's time, the teaser's and the "out now" clip's start and end times, and two to four characters for the cards.

Captions are composed in code, never by the model, in this order: Telugu, English, the fixed AI line, "Link in bio · YouTube", then the hashtags.
- The code enforces 2,200 characters and five hashtags.
- Stories get no caption.
- The fixed AI line and the picture tag are config values (`promote.ai_line_te`, `promote.ai_line_en`), so the owner can correct the Telugu once.

Video (1080x1920, H.264 High, yuv420p, 24 fps, closed GOP, AAC 48 kHz at 128 kb/s, moov atom at the front, no edit list):
- Encoder arguments: `-c:v libx264 -profile:v high -pix_fmt yuv420p -r 24 -g 48 -keyint_min 48 -sc_threshold 0 -flags +cgop -crf 20 -maxrate 10M -bufsize 20M -c:a aac -ar 48000 -b:a 128k -movflags +faststart -use_editlist 0`.
- Layout, version 1: the 16:9 picture scaled to 1080 px wide and centred over a blurred, enlarged copy of itself, with a title band on top and the AI tag in a corner for the whole clip.
  - A face-guided crop would need a reframer. `post/facelock.py` has face tracks but no reframing code.
- `teaser_reel.mp4`: 15 to 30 seconds of `11_master/movie.mp4`, between the agent's times snapped to subtitle edges. It keeps the film's own sound, has 0.5-second fades and ends on a 2-second card "Coming soon on YouTube · link in bio", with no date.
  - It is cut from the film, not the trailer, so the three Reels differ.
  - `plan_trailer` is weak at 15 seconds, and a new teaser stage would need a paid music cue. A film excerpt needs neither.
- `trailer_reel.mp4`: the whole trailer plus a 3-second end card "Full film on YouTube · link in bio". It is also the trailer story's video.
- `out_now_reel.mp4`: a second moment of the film, 10 to 20 seconds, plus "Now on YouTube · link in bio".
- Before the gate, `ffprobe` checks every file against Meta's limits.
  - Reels: 3 seconds to 15 minutes, at most 300 MB, at most 1920 px wide, 23 to 60 fps, at most 25 Mb/s.
  - Story video: 3 to 60 seconds, at most 100 MB.

Images (JPEG, quality 90, sRGB, under 8 MB; quality steps down if a file is too big):
- `poster_feed.jpg`, 1080x1350: a frame from the 1080p master at the agent's chosen time, cover-cropped, with `title_te` large, `title_en` and the AI tag. No date.
  - A keyframe is only 1376x768. A 4:5 crop of one would be stretched 1.76 times and look soft. A master frame is stretched 1.25 times.
- `poster_story.jpg`, 1080x1920: the same frame, with "<date> · 7 PM IST · YouTube".
- `char_<id>.jpg`, 1080x1350, two to four of them: from `library/characters/<asset>/refs/face/01_front.png`, with the Telugu and English names and "AI-generated character". All are 4:5, because a carousel crops every item to its first.
- Story images, 1080x1920: `teaser_story.jpg`, `count_2d.jpg`, `count_1d.jpg`, `count_0d.jpg`, `watch_now.jpg`, `thanks.jpg`, and `new_date.jpg` only after a move.
- `cover_<item>.jpg`, 9:16, one per Reel, with the titles and faces inside the centre safe area. They survive the 9:16 crop, the 3:4 grid tile and the documented 1:1 crop.
- Every file name is plain ASCII. Meta's reference says URLs outside the ASCII set fail.
- Telugu text reuses `post/cards.telugu_font()` and its line style, on new portrait canvases. `cards.py` itself is landscape-only.
- Without the Telugu font, an item renders in English only, and the gate says "Telugu font missing". The Dockerfile's font download becomes required.

### 3.4 The schedule

The schedule is laid out in code from T, so it is testable. The hours in brackets are for the default T of 19:00 IST.

| Item id | When | Kind | Asset | Waits for | Skipped if not posted by |
|---|---|---|---|---|---|
| `first_look` | T − 7 d (19:00) | image (feed) | `poster_feed.jpg` | — | the end of its IST day |
| `first_look_story` | T − 7 d + 30 min (19:30) | story image | `poster_story.jpg` | — | the end of its IST day |
| `characters` | T − 6 d (19:00) | carousel | `char_*.jpg` | — | the end of its IST day |
| `teaser` | T − 5 d (19:00) | Reel | `teaser_reel.mp4`, `cover_teaser.jpg` | — | the end of its IST day |
| `teaser_story` | T − 5 d + 30 min (19:30) | story image | `teaser_story.jpg` | — | the end of its IST day |
| `trailer_reel` | T − 3 d (19:00) | Reel | `trailer_reel.mp4`, `cover_trailer.jpg` | the trailer public | the end of its IST day |
| `trailer_story` | T − 3 d + 30 min (19:30) | story video | `trailer_reel.mp4` | the trailer public | the end of its IST day |
| `count_2d` | T − 2 d (19:00) | story image | `count_2d.jpg` | — | the end of its IST day |
| `count_1d` | T − 1 d (19:00) | story image | `count_1d.jpg` | — | the end of its IST day |
| `count_0d` | T − 9 h (10:00) | story image | `count_0d.jpg` | — | T |
| `out_now` | T + 15 min (19:15) | Reel | `out_now_reel.mp4`, `cover_out_now.jpg` | the film public | T + 24 h |
| `watch_now` | T + 15 min (19:15) | story image | `watch_now.jpg` | the film public | T + 24 h |
| `thanks` | T + 1 d (19:00) | story image | `thanks.jpg` | the film public | the end of its IST day |
| `new_date` | the first tick after a re-approval | story image | `new_date.jpg` | — | 24 h after approval |

- The trailer's YouTube `publishAt` is T − 3 d − 15 min (18:45), so the trailer is public before its Reel's tick.
- `new_date` exists only when the release moved after something had posted.
- Times are stored as UTC ISO strings with their offset. The board renders them in IST and New York time.
- The planner refuses two feed posts or Reels on one IST day.
- No pre-release item posts after T.
- Containers are prepared ahead: videos 60 minutes before the slot, images and carousels 20 minutes before.
- An item posts on the first tick at or after its slot. Posts are 0 to 15 minutes late.

### 3.5 The publishing ledger: never posts twice

The file:
- `gs://<bucket>/<slug>/promote/ledger.json` is the campaign's only live state.
- Every write goes through `queue.update_text`, which writes only if nobody changed the object since it was read (the generation precondition), with content type `application/json`.
- The sweep, the control room and `produce()` all write it this way, so no edit is lost.
- `gs://<bucket>/queue/promotions.json` lists the live campaigns. A film leaves it when every item is final.

Its fields:
- Campaign: `version`, `slug`, `release_at`, `film {id, fingerprint, publish_at}`, `trailer {id, publish_at}`, `approved_sha`, `paused` (null, or who, why and when).
- Item: `id`, `kind`, `at`, `stale_after`, `needs`, `assets [{object, crc32c}]`, `cover`, `caption`, `alt_text`, `state`, `container_id`, `container_at`, `lease_until`, `attempts`, `last_error`, `media_id`, `permalink`, `posted_at`, `ai_label`.

States: `planned` → `prepared` (a container exists) → `publishing` (claimed) → `posted`. Side states: `waiting` (with the reason), `failed`, `skipped` (with the reason), `manual` (the owner posts it), `held`.

The rule that prevents a double post:
1. Before calling `media_publish`, a sweep moves the item to `publishing` with `lease_until` set 10 minutes ahead, in one conditional write. Only the writer that wins calls `media_publish`.
2. On success it records the media id. Then it reads the permalink, `media_product_type` and `is_ai_generated`, and marks the item posted.
3. A sweep that finds an item still `publishing` after its lease has run out reads the container's `status_code`. Meta's guide names this check for the case where `media_publish` returned no media id.
   - `PUBLISHED`: the item counts as posted. The media id comes from `GET /<IG_ID>/media` (feed and Reels) or `GET /<IG_ID>/stories`, matched by time. If no match turns up, the item reads "posted, id unknown".
   - `FINISHED`: the sweep publishes the same container again. Meta does not document that a container posts at most once. The Instagram test (section 2, step 12) checks it on the live API.
   - `IN_PROGRESS`: wait.
   - `ERROR` or `EXPIRED`: only now may a new container be made, and only after the same media and stories lookup shows that nothing was posted in that window.
4. A crash between creating a container and recording its id leaves an unused container. It expires unpublished after 24 hours. That is harmless.
5. A new container is never made while the item still has a container that is `FINISHED`, `IN_PROGRESS` or `PUBLISHED`.

### 3.6 The 15-minute sweep

Placement:
- `queue._tick` calls `promote.sweep.run()` first, before the library restore. The sweep needs nothing from the library, and posting on time matters more.
- The call sits inside `try`/`except`, under `_doing(tracker, "promote")`, and its result goes into `summary["promote"]`. A sweep error never changes the story step's outcome.
- With nothing due, a sweep makes a few small bucket reads (the campaign index, the Instagram record, any test request) and ends in about a second.
- Its time limit is 3 minutes. Unfinished work waits for the next tick.
- Executions overlap. Cloud Scheduler starts one every 15 minutes, even while another renders a film for hours. Every write is conditional, so two sweeps cannot both post.

Each sweep, in order:
1. **Test requests.** If the control room asked for a test (`settings/probe_request.json`), the sweep runs it and writes the result to `settings/instagram.json` or `settings/youtube.json`.
2. **Token upkeep**, on every tick, with or without a campaign (3.7).
3. **Each live campaign that is not paused, items in time order:**
   1. If Instagram is not connected, not tested, or needs a reconnect, nothing is attempted. Due items show that reason.
   2. When something is due, one YouTube read (`videos.list`, part `status`) covers the film and the trailer. The rules are in 3.8. A schedule changed outside the control room pauses the campaign.
   3. When something is due to publish, one read of `GET /<IG_ID>/content_publishing_limit` (fields `quota_usage`, `config`) follows. At the cap, items wait.
      - The cap is read, never hard-coded. Meta's pages say 100 in one place and 50 in another.
      - The field names are confirmed only in the analogous Threads documents. If the read fails, the sweep relies on the quota error instead.
      - A campaign makes about 13 posts over 8 days, far under either cap.
   4. **Prepare** due containers. The sweep checks each asset's checksum against the ledger; a changed asset puts the item on hold. It signs a 24-hour link with `gcs.signed_url(bucket, name, days=1)`, with no file name, so no "attachment" header is added. It then calls `POST /<IG_ID>/media` with `is_ai_generated=true` and the fields for the item's kind.
   5. **Read the status** of prepared containers (fields `status_code,status`).
   6. **Publish** ready items whose time has come and whose needs are met, through the claim in 3.5.
   7. **Skip** items past their limit. **Hold** the film's or trailer's items 2 hours after their time if YouTube still says private.
   8. **Rebuild** any container older than 23 hours, because containers expire after 24 hours. This only happens to an item that waited a day.
4. **Report.** Each problem writes one log line, `PROMO_ALERT {slug, item, reason}`. It never contains a token or a URL.

What each kind sends:

| Kind | Container calls |
|---|---|
| image | `image_url`, `caption`, `alt_text`, `is_ai_generated=true` |
| carousel | each child: `image_url`, `is_carousel_item=true`, `alt_text`, `is_ai_generated=true`; then the parent: `media_type=CAROUSEL`, `children`, `caption`, `is_ai_generated=true` |
| Reel | `media_type=REELS`, `video_url`, `caption`, `share_to_feed=true`, `cover_url`, `is_ai_generated=true` |
| story image | `media_type=STORIES`, `image_url`, `is_ai_generated=true` |
| story video | `media_type=STORIES`, `video_url`, `is_ai_generated=true` |

Why the links last 24 hours:
- Meta fetches an image during the create call. A failure comes back on that call.
- Meta fetches a video while it processes it, after the call. A failure shows only later, as `status_code=ERROR`.
- So a video link must stay valid until the container is `FINISHED`, not just during the call.
- 24 hours covers that, and stays inside the 7-day limit for links signed with a service-account key.
- The bucket keeps public access prevention. A signed link still works with it.

### 3.7 Token handling

Connect (the same pattern as Connect YouTube):
- `GET /instagram/connect` requires sign-in. It sets a signed `ig_state` cookie (path `/instagram`, 600 seconds, httponly, samesite=lax). It redirects to `https://www.instagram.com/oauth/authorize` with the Instagram app ID, `redirect_uri=<public base>/instagram/callback`, `response_type=code`, `scope=instagram_business_basic,instagram_business_content_publish` and the state.
- `GET /instagram/callback` requires sign-in. It:
  1. checks the state;
  2. strips a trailing `#_` from the code, which Meta's guide warns about;
  3. calls `POST https://api.instagram.com/oauth/access_token` with form fields, and gets a short-lived token (the code is valid for one hour and works once);
  4. calls `GET https://graph.instagram.com/access_token?grant_type=ig_exchange_token`, on the server with the app secret, and gets a 60-day token;
  5. calls `GET /me?fields=user_id,username,account_type`;
  6. stores `{access_token, user_id, expires_at, refreshed_at}` with `secrets.add_version("INSTAGRAM_TOKEN_JSON", …)`, and checks that it returned True;
  7. writes `settings/instagram.json`, with no token in it;
  8. warns when the account type is not BUSINESS.
- `POST /instagram/disconnect` clears the record. The runner then posts nothing.
- To revoke the token itself: in Instagram, Settings → Website permissions → Apps and websites → Remove.

Refresh, by the runner, on every tick, independent of campaigns:
- The sweep reads `settings/instagram.json`, which holds no token.
- If `refreshed_at` is 7 or more days old, the sweep claims the refresh in that record (`refresh_until`, 10 minutes ahead, in a conditional write). A second execution then leaves it alone.
- It reads the secret first. If the secret's `refreshed_at` is under 7 days old, an earlier record write must have failed, and it only repairs the record.
- Otherwise it calls `GET https://graph.instagram.com/refresh_access_token?grant_type=ig_refresh_token`. Meta allows this when the token is at least 24 hours old and still valid.
- It stores whatever token comes back, with the new `expires_at` and `refreshed_at`, through `secrets.add_version`. The documents do not promise that the string stays the same.
- It checks the True or False result. A False goes on the card, and the record is not moved on.
- After a good write it destroys versions older than the newest two. This is a new soft-failing helper in `ops/secrets.py`.
- The card shows the last refresh and its result. It warns when fewer than 14 days are left.
- Error 190 sets `needs_reconnect`. Every item waits, and the board shows **Reconnect Instagram**.
- A token not refreshed for 60 days can no longer be refreshed. With a weekly refresh, that happens only if the runner is stopped that long, or the token is revoked.

Keeping the token out of logs:
- Graph calls send the token in an `Authorization: Bearer` header, as Meta's examples do.
- Only the exchange and the refresh carry it in the query string, as Meta documents them.
- No request URL is ever logged.
- The client never raises the `requests` library's `HTTPError`, whose message contains the URL. It builds its own error from the JSON body.
- Every error message passes through `redact()`.

### 3.8 The YouTube side

- `build_metadata(..., publish_at=None)` sets `privacyStatus=private` and `status.publishAt` when it gets a time. `containsSyntheticMedia=true` and `selfDeclaredMadeForKids=false` stay as today.
- The film's `publishAt` is T. YouTube allows `publishAt` only on a private video that was never published.
- The trailer's `publishAt` is T − 3 d − 15 min, or now + 15 minutes if that has passed. A time in the past publishes at once.
- After each upload, `reports/youtube.json` is copied to the bucket immediately (the new `after_record` hook). A tick that dies before its end-of-tick backup can then never upload the same cut twice.
- The read-back: `videos.list` (part `status`) must show the film as private with `publishAt` equal to T. Otherwise the run is `blocked:publish`, and no campaign starts.
- Re-scheduling for a move: `youtube.schedule(video_id, time)` reads the whole `status` part and writes it back with only `privacyStatus` and `publishAt` changed. `videos.update` replaces the whole part, so a partial write would reset the other fields.
- **Hold release** is done by the control room itself, at once (3.9). It is the same call with no `publishAt`. It is refused once the film is public.

The sweep's checks of the film (F) and the trailer (R):
- F private, with `publishAt` equal to T: on schedule.
- F private with another `publishAt`, or none: pause, "the film's schedule changed outside the control room".
- F public more than 10 minutes before T: pause, "the film went public early".
- F public from 10 minutes before T on: released. The film's items may post.
- F still private 30 minutes after T: an alert. The film's items are held after 2 hours, with "YouTube did not publish the film at T".
- R public: the trailer's items may post.
- YouTube unreachable, or its token dead: the film's and trailer's items wait, with the alert "Reconnect YouTube".
- Premieres are not used in version 1. A Premiere set up in Studio changes the schedule, so the campaign pauses and says why.

### 3.9 The control room

Home, the Instagram card:
- It shows the @handle, the account type, the token's expiry on both clocks, the last refresh and its result, the last test and its result, and the publishing quota used.
- Buttons: Connect, Test Instagram, Post the test story, Disconnect.
- The YouTube card gains a **Test YouTube scheduling** button and its result.

The film page, Instagram section:
- Before approval, it shows the gate view (3.2).
- While the campaign is live, it shows the schedule: each item's time on both clocks, surface, thumbnail, caption, state (with the permalink, or the error), and what it waits for.
- It also shows the next item due, and YouTube's state for the film and the trailer (private and scheduled, or public).
- Per item: **Retry** (resets the attempts; only before the item's limit), **Skip**, **Post by hand** (a download link, the caption to copy, and the URL for a link sticker) and **Mark posted** (with an optional permalink).
- Per campaign: **Pause**, **Resume**, **Hold release**, **Move release**.
  - Hold release calls YouTube at once, and pauses the campaign. If the call fails, the page says to change the film's visibility in Studio straight away.
  - Move release is a hold, then a request with the new time, then the row set to `retry` and a tick started. The runner re-drafts the remaining items and holds the gate. On approval, YouTube is re-scheduled and the campaign resumes.
- After a move, it lists the posted items whose captions carry the old date, each with the new caption to paste in the app (the post → ⋯ → Edit).
- The pipeline strip gets an "Instagram" stage between Delivery and YouTube.
  - It reads `skipped` when promote is none, `awaiting` at the gate, and `done` once the campaign is live, with text such as "5 of 13 posted".
  - Those states keep `films.in_order` from marking a finished YouTube stage as stale.
- The New story form and Save row get the Instagram field (none or campaign).
- Turning the field off does not stop a live campaign. Pause does.
- Every action is a conditional write to the ledger, to `request.json` or to the queue sheet. Hold release adds one YouTube call.
- Approve and Move start a tick at once, as Approve does today.

### 3.10 Failure handling

How the sweep classifies a failure:

| Failure | How it shows | What the runner does |
|---|---|---|
| Meta could not fetch an image | error 9004 / 2207052 (or 2207003, a slow source) on the create call | a new link and a new container on the next tick, up to 3 attempts, then `failed` |
| Meta could not fetch a video | `status_code=ERROR`, with 2207052, 2207003, 2207076 or 2207082 in `status` | the same |
| Not ready to publish | 9007 / 2207027 | wait |
| Container ERROR or EXPIRED | the status read | a new container, after the "already posted?" lookup (3.5) |
| Token invalid or expired | error 190 | every item waits; the board shows Reconnect Instagram |
| Publishing cap reached | 9 / 2207042, or `quota_usage` at the total | wait |
| Throttled (Meta's Business Use Case limit) | the throttling error | wait |
| AI flag rejected | an unknown-parameter error on `is_ai_generated` | the item goes to `manual`; the owner posts it in the app with Meta's AI label on |
| AI flag ignored | the read-back of `is_ai_generated` is false after posting | an alert: "posted without the AI label; delete it in the app and post it by hand" (the label cannot be changed after posting) |
| Asset changed after approval | the checksum differs | `held` |
| YouTube schedule changed | the sweep's YouTube read | the campaign pauses |
| Anything else | — | retry up to 3 attempts, then `failed` |

- The error codes come from vendor and community reports. Meta's error-code page was not read.
- Every failed item shows its error, with the token removed, and its buttons.
- Every failure writes a `PROMO_ALERT` log line for the optional e-mail.
- Nothing on the Instagram side can stop or move the YouTube release. Only Hold release and Move release can.

The hard cases:
- **The owner delays the release after stories went out.** Hold release acts at once. Posted stories expire within a day. Feed posts and Reels carry no date on the picture. Captions with the old date are listed, with replacements to paste in the app. The amended plan adds a "new date" story to the remaining items, and the owner approves it again.
- **An item fails after its time.** It becomes `skipped` at its limit. A pre-release item never posts after T. The owner can use Post by hand, or let it go.
- **The film is still private when a story would say "watch now".** The film's items wait until YouTube reports it public. After 2 hours they are held, with the reason.
- **A tick dies in the middle of publishing.** The lease and the container's status settle it (3.5).
- **The token expires.** The weekly refresh prevents it. If it happens anyway, error 190 brings up Reconnect, items wait, and the ones whose day passes are skipped.
- **A re-cut while a campaign is live.** The promote step refuses until Hold release. The new cut is then drafted with the posted items carried over. The old upload stays private and unscheduled, and the board reminds the owner to delete it in Studio.
- **The approval comes late.** Items already past show as "will be skipped". If T is under 24 hours away, the runner re-drafts with the next release day.

### 3.11 Costs

- **Claude:** one structured call per draft, made only when the inputs change. Caption edits make none. It is logged in `reports/llm_usage.jsonl`, like the other planning agents. Like them, it is not counted against the film's cap. It costs cents.
- **Rendering:** local ffmpeg and Pillow, inside the job that is already running: a few minutes of vCPU.
- **Storage:** about 100 to 300 MB of assets per film, plus internet egress each time Meta fetches a file (about 0.2 to 0.4 GB per campaign, with retries). A few cents.
- **Secret Manager:** one new version a week, old versions destroyed, and reads only when needed. Negligible.
- **Meta's Graph API:** free.
- **YouTube Data API:** quota only. The two uploads happen today already; this adds a few small reads and updates, and one upload per YouTube test.
- **Cloud Run:** no new executions. The sweep rides the existing 15-minute ticks.
- **Optional alert e-mail:** Cloud Monitoring's price, the owner's choice.
- **No paid scheduler.** A paid service such as Ayrshare (from about $149 a month) would be considered only if Meta blocked the app, and only with the owner's approval.

---

## 4. Build plan

Patterns to keep from the repo:
- module-level hooks that tests replace (`gcs.STORAGE_FACTORY`, `gcs.RUN`, `queue.TRACKER_FACTORY`);
- helpers that fail soft to None or False;
- gates as marker files, checked against `queue.GATES`;
- conditional writes through `queue.update_text`;
- `ReplayLLM` fixtures for agents;
- ffmpeg tests skipped when ffmpeg is missing.

Fakes used by the tests:
- `FakeGraph`: records every request, and replays scripted answers, including a sequence of container statuses per container.
- A fake Secret Manager client.
- A fake YouTube client.
- Fake blobs that raise `PreconditionFailed` once.

Steps, in order:

0. **Check the live documents first** (no code).
   - On developers.facebook.com, re-check: `is_ai_generated` on `POST /<IG_ID>/media` for Instagram Login and the version that brings it (the SDK has it on the Instagram Login node from 26.0.0); Stories on `graph.instagram.com`; the fields of `content_publishing_limit`; the container `status` text.
   - On developers.google.com, re-check `publishAt` and the unverified-project sentence.
   - Record what changed in the design before building.

1. **`src/nraiorbit/service/queue.py`, the sheet and the gates.**
   - `STORY_COLUMNS` gains `promote` after `publish`.
   - `apply_row` writes `policy.promote` (`none` or `instagram`).
   - `GATES` gains `promote` just before `publish`.
   - Tests (`tests/test_service.py`): an old sheet without the column loads, and is saved with it; `apply_row` writes the value; a tick's row write keeps the owner's value; the gate order.

2. **`src/nraiorbit/schemas.py`.**
   - `PromoCopy` (the agent's answer), `PromoItem`, `PromoCampaign`, `PromoLedger`, and `compose_caption()`.
   - Validators: at most five hashtags; captions of at most 2,200 characters; no caption on stories; alt text on images only; carousels of 2 to 10 children; excerpts inside the film and of the right length.
   - Tests (`tests/test_promote_schemas.py`): six hashtags rejected; a story caption rejected; eleven children rejected; caption order Telugu, English, AI line, link line, tags.

3. **`src/nraiorbit/promote/__init__.py` and `promote/schedule.py`.**
   - `IST`, the template of 3.4, `default_release(now, cfg)`, `plan(release_at, now, posted_ids)`, the skip limits, the one-feed-post-a-day check and the quarter-hour check.
   - Tests (`tests/test_promote_schedule.py`): the default lands on a Tuesday, Wednesday or Thursday at 19:00 IST, at least 8 days away; each offset; the skip limits; posted items carried over; `new_date` added only after something posted; two feed items on one day refused; times round-trip with their offset; New York rendering across 1 November 2026.

4. **`src/nraiorbit/promote/graph.py`, the Graph client.**
   - `HTTP_FACTORY` (tests inject a fake), `BASE = "https://graph.instagram.com/v26.0"` (configurable), the bearer header, `authorize_url`, `exchange_code`, `long_lived`, `refresh`, `me`, `create_container(kind, …)`, `status`, `publish`, `publishing_limit`, `recent_media(since)`, `recent_stories(since)`, `media_fields`, `GraphError(code, subcode, kind)`, `classify()` and `redact()`.
   - Tests (`tests/test_promote_graph.py`, with `FakeGraph`): the exact fields per kind; `is_ai_generated=true` on every container, children included; the token sent in the header and absent from URLs, except for the exchange and the refresh; the classification table in 3.10; video fetch errors found in the `status` text; no token in any raised message.

5. **`src/nraiorbit/ops/secrets.py` and `promote/token.py`.**
   - `secrets.destroy_old_versions(name, keep=2)`, failing soft.
   - `token.load`, `token.save` (checks the result of `add_version`), `token.upkeep(record, secrets, graph, now)` with the claim and the repair path, and `token.days_left`.
   - Tests (`tests/test_promote_token.py`): no refresh before 7 days; a second claimant backs off; the repair path when the secret is newer than the record; a False from `add_version` is reported and the record is not moved on; 190 sets `needs_reconnect`; old versions are destroyed only after a good write.

6. **`src/nraiorbit/publish/youtube.py` and `publish_youtube` in `service/produce.py`.**
   - `build_metadata(..., publish_at=None)`, `schedule(video_id, publish_at_or_none, creds)` (read the status part, change two fields, write it back), `status(ids, creds)` and `delete(video_id, creds)`.
   - `publish_youtube(root, privacy, publish_at=None, trailer_publish_at=None, after_record=None)` stores `publish_at` in the record, and calls `schedule` when this cut is already uploaded but its time differs.
   - Tests (extend `tests/test_trailer_publish.py` and `tests/test_produce.py`): `publishAt` set and the video private; a past trailer time moved to now + 15 minutes; `schedule` re-sends the whole status part; the existing two-argument call still works (the current test's fake takes two arguments); `after_record` runs after each upload.

7. **`src/nraiorbit/promote/art.py`.**
   - Portrait canvases with `cards.telugu_font()` and the `_line` style: `poster`, `character_card`, `story_card`, `end_card`, `cover`, the AI tag, and `save_jpeg` (RGB, sRGB, quality 90, stepping down below 8 MB).
   - Tests (`tests/test_promote_art.py`): sizes 1080x1350 and 1080x1920; JPEG and RGB; under 8 MB; the AI tag drawn on every kind; the English-only fallback when `telugu_font` returns None.

8. **`src/nraiorbit/promote/video.py`.**
   - `vertical(src, out, title_png)`, `excerpt(movie, start, end, out)`, `with_end_card(video, card_png, out, seconds)`, the shared encoder arguments, and `check(path, kind) -> list[str]`.
   - Tests (`tests/test_promote_video.py`, skipped without ffmpeg): from a 1920x1080, 24 fps, 40-second test source with a tone, the output is 1080x1920, yuv420p, AAC at 48 kHz, with the moov atom first; an excerpt is within 30 seconds; `check` passes; a 70-second input fails the story check.

9. **`src/nraiorbit/agents/promoter.py` and `src/nraiorbit/prompts/promoter.md`.**
   - `PromoterAgent(Agent)` with `draft_copy(bible, outline, cast, subs_te, subs_en, slots, release_text) -> PromoCopy`, using `project_context` and `load_prompt`.
   - The prompt asks for Telugu first, then English; no misleading claims; three to five specific hashtags; excerpt times on subtitle edges; no spoiler of the ending.
   - Tests (`tests/test_promoter.py`, with `ReplayLLM`): one call per draft; ids map onto the slots; an unknown id is rejected; excerpt times outside the film are rejected.

10. **`src/nraiorbit/promote/campaign.py`.**
    - `draft(pipeline, bucket, now, llm)`, the reuse rule (inputs hash), applying `request.json` (edits, drops, move), `sha`, and `approved_sha(pipeline)` (reads the JSON line in `reviews/promote.approved`).
    - Tests (`tests/test_promote_campaign.py`, with `ReplayLLM`, the fake storage hook and a small project): the files exist; unchanged inputs make no Claude call; a caption edit changes the `sha` and makes no call; a dropped item is gone; posted items carry over.

11. **`src/nraiorbit/promote/ledger.py`.**
    - `activate(bucket, slug, campaign)` (create, or merge keeping posted items), `load`, `change(fn)`, `claim(item_id, lease)` returning True or False, `index_add` and `index_remove`.
    - Tests (`tests/test_promote_ledger.py`, with `update_text`'s local-file mode and a fake blob that raises `PreconditionFailed` once): two claimants, one winner; an owner edit and a sweep edit both survive; a merge keeps posted items; a second activation of the same campaign changes nothing.

12. **`src/nraiorbit/promote/probe.py`.**
    - `instagram_probe`, `post_test_story` and `youtube_probe`, with their test media rendered on the runner (a 1080x1920 card and a 5-second clip).
    - Tests (`tests/test_promote_probe.py`, with `FakeGraph` and a fake YouTube client): results written to the settings record; the second publish of the same container is expected to fail, and a success fails the test; a false `is_ai_generated` read-back fails the test; a YouTube lock is detected when the clip stays private; the test clip is deleted even when a step fails.

13. **`src/nraiorbit/promote/sweep.py`.**
    - `run(bucket, *, now=None, graph=None, secrets=None, youtube=None, deadline_s=180, log=print) -> dict`.
    - Tests (`tests/test_promote_sweep.py`, with `FakeGraph`, fake secrets, a fake YouTube client and a fixed clock):
      - an image posts once;
      - a video's container is created 60 minutes early, is `IN_PROGRESS` on the next tick, and posts at its time;
      - a crash after publishing (`publishing`, lease expired, container `PUBLISHED`) is marked posted with no second publish call;
      - a crash with the container `FINISHED` publishes the same container, never a new one;
      - `EXPIRED` leads to the lookup, then a new container;
      - a video fetch error found in `status` re-signs the link;
      - three failures end in `failed`;
      - 190 makes every item wait;
      - the quota defers;
      - a countdown past its day is skipped;
      - nothing pre-release posts after T;
      - film items wait for YouTube to report the film public, and are held 2 hours after T;
      - a changed `publishAt` pauses the campaign;
      - a changed asset holds the item;
      - an AI-flag rejection goes to `manual`;
      - the token refresh runs with no campaign live;
      - the time limit is respected;
      - a paused campaign is untouched;
      - a test request is answered.

14. **`src/nraiorbit/service/queue.py`, the tick.**
    - `_tick` calls the sweep first, inside `try`, with `_doing(tracker, "promote")`, and puts the result in `summary["promote"]`.
    - Tests (extend `tests/test_service.py`): the sweep runs on every tick, even while a story row is claimed by another execution; it runs before the library restore; a sweep exception does not change the story outcome.

15. **`src/nraiorbit/service/produce.py`.**
    - `RUNNER_GATES = ("cost", "faces", "promote", "publish")`, `promote` in `policy_of`, the promote step, the approval check, the scheduled publish with its read-back, `blocked:publish` for campaign films, and the activation.
    - Tests (extend `tests/test_produce.py`):
      - with promote none, behaviour is unchanged and the existing tests pass;
      - with promote instagram, the run holds at `promote` after deliver;
      - after approval it uploads once with `publishAt`, reads it back, and activates the ledger once;
      - a failed upload ends as `blocked:publish` with no ledger;
      - a changed `sha` holds the gate again;
      - T under 24 hours away leads to a re-draft;
      - a live campaign plus a new cut holds the gate and uploads nothing;
      - `gates: auto` still holds at `promote`;
      - publish not public reports "needs YouTube public" and runs the old flow.

16. **`src/nraiorbit/ops/backup.py` and `src/nraiorbit/pipeline.py`.**
    - `FOLDERS["promote"] = "12_promote"`, added to the default sets.
    - `Pipeline.invalidate` also drops the `promote` markers and `12_promote/campaign.json` for any stage up to `trailer`. It never touches the bucket ledger.
    - Tests (extend `tests/test_gates.py` and `tests/test_qa_release_backup.py`).

17. **The control room.**
    - `src/nraiorbit/ui/app.py`: `Settings` gains `instagram_app_id` and `instagram_app_secret`. The routes `/instagram/connect`, `/instagram/callback`, `/instagram/disconnect` and `/instagram/test` (it writes the test request and starts a tick). The film routes `/film/{slug}/promote/request` (edits, drops, move), `/film/{slug}/promote/item`, `/film/{slug}/promote/pause` and `/film/{slug}/promote/hold`. The New story form and Save row accept `promote`. `RESERVED_SLUGS` gains `instagram`.
    - `src/nraiorbit/cli.py`, `cmd_ui`: read `INSTAGRAM_APP_ID` and `INSTAGRAM_APP_SECRET` from the environment.
    - `src/nraiorbit/ui/films.py`: `STAGES` gains `("promote", "Instagram")` after Delivery, with the states in 3.9; `GATES` and `PHASES` gain `promote`; a new `promote_view(ledger)`.
    - Templates: the Instagram card in `home.html`; the Instagram section, the gate text and `promote` in the hard-coded gate list in `film.html`; a paragraph about Instagram in `policy.html`; a filter that prints a time in IST and New York time.
    - Tests (extend `tests/test_ui.py`): Connect sets the cookie and redirects with the right scopes; the callback rejects a bad state; it exchanges through `FakeGraph`, stores through fake secrets, and the record holds no token; a non-BUSINESS account gets a warning; Approve accepts `promote`; the request route writes `request.json` and sets `retry`; Hold calls the fake YouTube client and pauses the ledger; item actions write the ledger; a film with promote none shows the Instagram stage as skipped and YouTube as done, not stale.

18. **`src/nraiorbit/cli.py`.** `nraiorbit promote status <slug>`, `nraiorbit promote sweep [--dry-run]` and `nraiorbit promote probe [--youtube]`.
    - Tests: a smoke test with the fakes.

19. **Config, deploy and docs.**
    - `config/pipeline.yaml` gets a `promote:` block: `lead_days: 7`, `release_time: "19:00"`, `release_days: [tue, wed, thu]`, `retries: 3`, `graph_version: v26.0`, `fixed_hashtags`, `ai_line_te`, `ai_line_en`. There is no `auto` key.
    - `projects/_template/pipeline.yaml` gets `policy.promote: none`.
    - `deploy/service/Dockerfile`: the Telugu font download becomes required (drop `|| true`).
    - Both deploy READMEs: the additive `gcloud` commands in section 2 and the IAM roles.
    - Update `docs/TOOLS.md` (the planned `publishAt` line) and `docs/PROJECT_STATUS.md`.
    - Tests: the config default test in `tests/test_service.py`.

---

## 5. What stays manual, and the risks

### Not possible through the API, so it stays manual or is left out

- **Story stickers.** Link, countdown, poll, location and "Add Yours" stickers cannot be published through the API. Stories say "link in bio". An owner who wants a sticker uses Post by hand on that item.
- **The profile link.** No API edits it. It is set once to the YouTube channel. Pointing it at the film at T is optional, and by hand.
- **Pinned comments and replies.** No pin endpoint exists. Pinning and replying happen in the app.
- **Music.** Only original audio is supported. `audio_name` only renames it. The film's own score is used.
- **Scheduling.** The API has no scheduled-publish parameter. Times are as precise as the 15-minute tick: posts go out 0 to 15 minutes after their slot.
- **Changes after posting.** No caption edit and no delete exist on the Instagram Login path. A wrong post is fixed or removed by hand in the app. That is why the gate matters.
- **A YouTube Premiere.** It can only be set up in Studio, and is not used in version 1.
- **Story limits.** Stories take no caption, alt text or collaborators.
- **Not used:** trial Reels, product tags, paid-partnership labels and polls.
- **Insights.** They are read in the app. The insights scope is not requested.
- **The deauthorize callback.** It is not built. If the owner removes the app in Instagram, the next call returns 190 and the board asks for a reconnect.
- **Brand-new Instagram accounts** may not get the link sticker at all. That affects only the optional manual stickers.

### Risks, and how each is handled

1. **The documents are not live.** Every Meta fact comes from extracts, the Postman collection, the SDK, or copies saved on 2025-12-14. Step 0 re-checks them, and the two tests settle the rest.
2. **The AI flag.** `is_ai_generated` is in Meta's official SDK (on the Instagram Login node from 26.0.0, 2026-08-06), but it was not seen on the live reference.
   - A rejected flag sends the item to manual.
   - A silently ignored flag is caught by the read-back after posting, and first by the Instagram test.
   - The label apparently cannot be changed after posting, so a post without it must be deleted and re-posted by hand.
3. **Stories on this login path.** The Content Publishing guide's Story section covers both login paths, and Meta's Postman collection creates a story on `graph.instagram.com`. Creator accounts are undocumented. The design uses a Business account, and the test posts one real story.
4. **The YouTube lock.** Google's newest reference text (revision 20261012) says unverified projects are not restricted to private, while older help and changelog text says they are. The YouTube test settles it without making anything public.
5. **Signed links.** That Meta accepts long V4 signed links is an inference, backed by one outside project that used one-hour GCS links for Reels. The test uses real 24-hour links for an image and a video.
6. **The publishing cap.** It is 50 or 100 a day, depending on the page. The runner reads it. A campaign needs about 13 posts over 8 days.
7. **The token.** It dies if the runner stops for 60 days, or if access is revoked or the password changes. The board shows the expiry and asks for a reconnect.
8. **The Meta dashboard.** One October 2026 report describes a blank Instagram panel for the new use case. The older "Other → Business" path is the fallback.
9. **Spam and originality signals.** Three Reels from the same footage would look repetitive. The Reels use different moments of the film, at most one feed item a day, at most five hashtags, and a different caption per item.
10. **Real people.** An AI character that resembles a real actor can draw impersonation reports. The gate's checklist asks the owner to look for that.
11. **Telugu written by the model.** The owner reads and edits every caption at the gate. The fixed AI line is a config value checked once.
12. **The test story is public for 24 hours.** It is a plain studio card, and the owner deletes it right after the test.
13. **Overlapping executions.** Every write is conditional, and the publish claim has a lease.
14. **The YouTube token.** The release checks depend on it. The consent screen must be "In production".
15. **Asset quality.** The vertical layout is a blurred fill, not a reframe. The Godavari trailer is 29.9 seconds against a 45-second target. Telugu depends on the font being in the image.
16. **The Graph API version.** v26.0 has no expiry date set yet. The version is a config value.
17. **The deploy itself.** Re-running the setup scripts would drop secrets and environment variables. Section 2 uses additive commands only.

---

## 6. Sources

Meta, official (none read live; dates as noted):
- Content Publishing guide: https://developers.facebook.com/docs/instagram-platform/content-publishing/ — copy saved 2025-12-14 (its alt_text note is dated 2025-03-24); search extracts 2026-10-19. Host table, the 100 and 50 caps, "media on a public server at the time of the attempt", resumable upload only for Facebook Login, the Story section, container status codes, and the troubleshooting text on a missing media id.
- IG User Media reference: https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-user/media/ (also at https://developers.facebook.com/documentation/instagram-platform/instagram-graph-api/reference/ig-user/media) — copy saved 2025-12-14; extracts show "updated 2026-09-28" and v26.0 examples. 24-hour container expiry, 400 containers a day, ASCII URLs, Reel and Story specs, no stickers, user tags on stories (2025-07-09).
- Business Login for Instagram: https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/business-login — copy saved 2025-12-14. Authorize, code exchange, long-lived token, refresh rules.
- Create a Meta App for Instagram Platform: https://developers.facebook.com/docs/instagram-platform/create-an-instagram-app/ — copy saved 2025-12-14 ("Other → Business" path; the added account must be public); the current use-case flow from extracts of 2026-10-20.
- App Review for Instagram API: https://developers.facebook.com/docs/instagram-platform/app-review — copy saved 2025-12-14 (an app only for a business you own or manage, Instagram Login, Standard Access: App Review not required).
- Instagram API with Facebook Login: https://developers.facebook.com/documentation/instagram-platform/instagram-api-with-facebook-login — copies up to 2026 (Stories only for business accounts).
- Instagram API with Instagram Login: https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login — copy saved 2025-12-14 (no Facebook Page needed).
- Instagram Platform overview: https://developers.facebook.com/docs/instagram-platform/overview/ — updated 2026-09-16.
- IG Media reference (update and delete): https://developers.facebook.com/docs/instagram-platform/reference/instagram-media/ — undated.
- Graph API versions: https://developers.facebook.com/docs/graph-api/changelog/versions/ — v26.0 released 2026-07-29.
- Rate limiting: https://developers.facebook.com/docs/graph-api/overview/rate-limiting/ — undated.
- Developer Policies: https://developers.facebook.com/devpolicy/ — last updated 2026-02-03.
- Meta Business SDK for Python: https://pypi.org/project/facebook-business/ — 25.0.3 (2026-07-17), 26.0.0 (2026-08-06), 26.0.2 (2026-09-21); `is_ai_generated` on media creation.
- Meta's "Instagram API" Postman collection, Instagram Login → Publish Content → Create a video container (a STORIES container on graph.instagram.com) — examples 2025-08 to 2025-09; copy in fbsamples/messenger-platform-samples.
- Labeling AI content: https://about.fb.com/news/2024/04/metas-approach-to-labeling-ai-generated-content-and-manipulated-media/ (2024-04); https://about.fb.com/news/2026/02/meta-prepares-for-2026-us-midterms/ (2026-02); https://transparency.meta.com/governance/tracking-impact/labeling-ai-content/ (undated).
- Spam policy: https://transparency.meta.com/policies/community-standards/spam/ — current, undated.
- Link stickers: https://about.instagram.com/blog/announcements/expanding-sharing-links-in-stories-to-everyone (2021-10-27); https://help.instagram.com/2281642782069520/ (undated).
- Instagram brand rules (app names): https://www.meta.com/brand/resources/instagram/instagram-brand/ — undated.

Google, official:
- YouTube videos resource, insert and update: https://developers.google.com/youtube/v3/docs/videos , https://developers.google.com/youtube/v3/docs/videos/insert (reported updated 2026-10-08), https://developers.google.com/youtube/v3/docs/videos/update (undated).
- YouTube Data API discovery document, revision 20261015: https://www.googleapis.com/discovery/v1/apis/youtube/v3/rest — fetched 2026-10-19; the "not restricted to private" sentence first appears in revision 20261012.
- Revision history: https://developers.google.com/youtube/data/revision_history — the 2020-07-28 audit entry; `containsSyntheticMedia` added 2024-10-30.
- Videos locked as private: https://support.google.com/youtube/answer/7300965 — undated.
- Premieres: https://support.google.com/youtube/answer/9080341 — undated.
- Disclosing altered or synthetic content: https://support.google.com/youtube/answer/14328491 — undated; https://blog.youtube/news-and-events/improving-ai-labels-viewers-creators/ (2026-05-27).
- Signed URLs (V4, at most 7 days): https://docs.cloud.google.com/storage/docs/access-control/signed-urls — current.
- Secret Manager best practices: https://docs.cloud.google.com/secret-manager/docs/best-practices — current.

Secondary and third-party (used only where marked):
- Five-hashtag cap: https://www.socialmediatoday.com/news/instagram-implements-new-limits-on-hashtag-use/808309/ — 2025-12.
- 3:4 profile grid: https://www.businesstoday.in/technology/news/story/instagram-head-announces-big-changes-3-minute-reels-and-new-look-for-profile-grid-461527-2025-01-21 — 2025-01-21.
- Tester invite path: https://docs.postiz.com/self-host/providers/instagram ; https://developers.chatwoot.com/self-hosted/configuration/features/integrations/instagram-via-instagram-business-login — 2025 to 2026.
- Blank Instagram panel report: https://github.com/ryanlaiyanip-ctrl/chatmany/issues/5 — about 2026-10-09.
- GCS signed links handed to Meta for Reels: https://github.com/A-Baji/ClipMorph/issues/249 — 2026-10-08 (replaced by resumable upload in v0.6.0, 2026-10-14).
- Video fetch errors seen through the container status: https://github.com/gitroomhq/postiz-app/issues/1472 (also issue 2000 and PR 2261) — 2025 to 2026.
- A developer report of the new YouTube sentence: https://github.com/fernandolisboa/bardo/issues/141 — 2026-10-13.
- Posting times in India (blog consensus): https://www.wscubetech.com/blog/instagram-post-time/ ; https://mydigitalsevak.in/blog/best-time-to-post-instagram-india/ — 2026.
- Ayrshare (paid fallback only): https://www.ayrshare.com/docs/apis/post/social-networks/instagram — 2026.

Repository, read at commit 622e5db (2026-10-19), not changed:
- `src/nraiorbit/service/produce.py`, `service/queue.py`, `publish/youtube.py`, `release/gcs.py`, `release/deliver.py`, `ops/secrets.py`, `ops/backup.py`, `pipeline.py`, `ui/app.py`, `ui/films.py`, `ui/store.py`, `ui/templates/film.html`, `post/cards.py`, `agents/base.py`, `llm.py`, `costing.py`, `cli.py`, `config/pipeline.yaml`, `projects/_template/pipeline.yaml`, `deploy/service/{setup.sh,Dockerfile,README.md,cloudbuild.yaml}`, `deploy/ui/{setup.sh,Dockerfile,README.md,cloudbuild.yaml}`, `tests/`.
