# Prompt for the next session

## Before you start (once)

1. **Environment variable:** `GEMINI_API_KEY` is your Google key (starts with `AIza`), on
   a Google Cloud project with **billing enabled**. Create it with
   [GOOGLE_CLOUD_SETUP.md](GOOGLE_CLOUD_SETUP.md) steps 1–7: account, paid billing,
   project, "Generative Language API", a key restricted to that API, and a budget alert.
2. **Network access** (cloud environment menu → Edit → Network access): allow
   `pypi.org`, `files.pythonhosted.org` and `generativelanguage.googleapis.com`. Keep
   "Allow package managers" ticked.
3. Start a **new session** on this repository, on branch
   `claude/telugu-ai-movie-generator-yhb6po`.
4. Paste the prompt below. **Never paste keys into the chat.**

## Prompt (copy everything in the box)

```text
Continue the NRAIORBIT Telugu film POC on branch claude/telugu-ai-movie-generator-yhb6po
(check it out if needed). The design and the automated pipeline are already built: read
README.md, docs/PROJECT_STATUS.md and docs/POC_RUNBOOK.md first.

I added my Google Cloud Gemini API key as the environment variable GEMINI_API_KEY. You may run
commands that use it: nraiorbit google models, identity build, run and retake.
Never print, log or commit the key.

Do this in order:
1. Check GEMINI_API_KEY is set without printing its value. If it is missing, stop and tell me.
2. pip install -e ".[dev,google]" (ffmpeg is preinstalled), then run the test suite.
   If pypi.org or Google's API is blocked, tell me which host to allow and stop.
3. Run `nraiorbit google models` (free). We chose quality first: Veo 3.1 standard
   (veo-3.1-generate-preview) at 1080p, the pro image model (gemini-3-pro-image), and the
   best Telugu-capable TTS model. If names differ (e.g. a "-preview" suffix) or a newer
   top-tier Veo / pro image / pro TTS model is listed, pick the highest-quality one and
   keep google.veo_tier matching its price tier.
4. From the repo root: `nraiorbit poc godavari5` (skip if projects/godavari5 exists),
   `nraiorbit estimate godavari5`, `nraiorbit shotpack export godavari5`. Put any
   model-name changes under google: in projects/godavari5/pipeline.yaml.
5. STOP before anything paid. Tell me in simple English: whether the key works, the
   models, the bill, and the paid steps (identity images ~$1.50, trailer test ~$56,
   full 5 minutes ~$121 more, each retake ~$3.33; one Veo attempt per shot). Send me
   projects/godavari5/shotpack/DIALOGUE.md to check the 13 Telugu lines. Ask me to
   approve budget caps (suggest $70 for the trailer test, $200 for the full run).
6. After I approve: `nraiorbit identity build godavari5 --yes --max-spend <cap>`, send me
   the character images (library/characters/*/refs/), and wait until I approve the faces.
7. Then `nraiorbit run godavari5 --only trailer --yes --max-spend <cap>`. Send me
   projects/godavari5/11_master/trailer.mp4, any shots in reports/review_queue.jsonl,
   and the "Actual spend" section of `nraiorbit estimate godavari5`.
   If I want a shot redone: `nraiorbit retake godavari5 <shot_id> --yes --max-spend <cap>`
   (or `--use N` to go back to an earlier attempt for free).
8. Only when I say so: `nraiorbit run godavari5 --yes --max-spend <cap>` for the full
   5 minutes. Send movie.mp4 and the final bill.

Rules: projects/ and library media stay gitignored. If a Google call fails on a model
name or parameter, fix it in configuration first. If code must change, add or adjust
tests, run the suite, then commit and push to the same branch. Update
docs/PROJECT_STATUS.md with what was done and what was spent.
```
