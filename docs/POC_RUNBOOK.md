# 5-minute POC runbook: "Godavari Simham"

Goal: produce a real 5-minute Telugu short and its trailer end to end on your setup
(MacBook Pro, Google Veo, DaVinci Resolve, AWS, Claude), and measure what it actually
costs.

The story, screenplay (10 scenes, 13 Telugu lines), shot plan (54 shots, each ≤ 8 s for
Veo) and music cue sheet are already written. They were written in Claude Code on your
subscription, so the run makes **no Claude API calls**. Everything else runs
automatically through your Google key:

| Step | Done by | Paid to |
|---|---|---|
| Character reference images (front, three-quarter, each costume) | Gemini image | Google |
| First frame of every shot with a lead character | Gemini image, using the reference images | Google |
| Video clip per shot | Veo (image-to-video) | Google |
| Telugu dialogue (13 lines, one voice per character) | Gemini TTS | Google |
| Edit, ducked music bed, −14 LUFS master, te/en subtitles, trailer | the pipeline (ffmpeg) on your Mac | — |
| Colour, final mix | DaVinci Resolve | — |

## The bill (quality-first: Veo 3.1 standard, native 1080p)

**Decision: Option A with no compromise on quality.**
- Video: Veo 3.1 standard (`veo-3.1-generate-preview`) at native 1080p. 1080p clips are 8 s each.
- Character photos and first frames: Gemini's pro image model.

These are the numbers `nraiorbit run` prints before it spends anything (one attempt per shot):

| Run | Veo | First frames | Voices | **Total** |
|---|---|---|---|---|
| Character reference images (once) | — | 11 images | — | **≈ $1.50** |
| **Trailer test**: `--only trailer` (17 shots) | 136 s → $54.40 | 13 → $1.74 | $0.02 | **≈ $56** |
| Full 5 minutes (54 shots; the trailer's 17 are reused) | 432 s → $172.80 | 30 → $4.02 | $0.02 | **≈ $177 total (≈ $121 after the trailer)** |
| Each retake of one shot (`nraiorbit retake`) | 8 s → $3.20 | $0.13 | — | **≈ $3.33** |

- `nraiorbit estimate godavari5` shows the planning range with retakes: $225–$518 for 1.3–3 attempts per shot.
- Lip-sync on AWS, when added, is about $2.
- **Full 1h45m feature at this quality:** about 1,400 shots × 8 s × $0.40 ≈ **$4,500 per pass**, or roughly **$6,000–$9,000** with retakes. Measure the real retake rate on this POC before committing.

**Spending safeguards:**
- A budget cap (`budget.max_usd`, $250 for this project) stops the run before any call that would cross it.
- Every paid call is logged to `reports/cost.jsonl`.
- Every Veo clip, first frame and voice line is kept, so a re-run, crash or budget stop never pays twice.

Prices are in `config/pricing.yaml`; the pro image price is in `projects/godavari5/pricing.yaml`. They come from third-party trackers captured 2026-10, so verify them on Google's and AWS's own pricing pages:
[Veo API per second](https://benchlm.ai/media-pricing/veo),
[Flow credits per plan](https://www.hooked.so/compare/veo-3-pricing),
[credits per clip](https://diyai.io/ai-tools/video-generation/google-veo-pricing/),
[AWS g6.xlarge, Mumbai](https://www.doit.com/compute/spot/ap-south-1/g6.xlarge),
[AWS g5.xlarge, Mumbai](https://www.doit.com/compute/spot/ap-south-1/g5.xlarge).

## Steps (automatic)

### 1. Install on your Mac

```bash
brew install ffmpeg python@3.12
git clone -b claude/telugu-ai-movie-generator-yhb6po https://github.com/rajireddys999/NRAIORBIT-AGENTICFILMPRODUCTIONS.git
cd NRAIORBIT-AGENTICFILMPRODUCTIONS
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,google]"
nraiorbit demo godavari          # 30-second sanity check, no keys needed
```

### 2. Google key

1. Create an API key in Google AI Studio (aistudio.google.com) on a project with
   **billing enabled**. Veo has no free tier.
2. Export it:
   ```bash
   export GEMINI_API_KEY=...
   nraiorbit google models          # check which Veo / image / TTS models your key can use
   ```
3. This project is set for the best quality: `veo-3.1-generate-preview` (Veo 3.1 standard),
   `gemini-3-pro-image` (pro image model) and `gemini-2.5-flash-preview-tts`. If
   `google models` shows a newer or higher-tier name (for example a "-preview" suffix on
   the pro image model, or a "pro" TTS model), put it into
   `projects/godavari5/pipeline.yaml` under `google:`. If you change the Veo model, keep
   `veo_tier` matching its price tier.

### 3. Create the POC and build the characters

```bash
nraiorbit poc godavari5
nraiorbit estimate godavari5                 # full bill table
nraiorbit identity build godavari5           # ~$1.50; asks before spending
```

**Look at the images** in `library/characters/*/refs/` before going further. Every shot
uses these faces. To regenerate one you don't like, delete it and run `identity build`
again; only missing images are regenerated.

### 4. Trailer test (about $56)

```bash
nraiorbit run godavari5 --only trailer
```

It prints the cost preview and asks before spending. It then generates the 17 trailer
shots and all 13 Telugu lines, and cuts:

- `11_master/movie.mp4`: the 5 minutes, with placeholder cards where shots aren't
  generated yet
- `11_master/trailer.mp4`: cut only from generated shots

Watch the trailer. Check `reports/review_queue.jsonl` for any shot Veo's safety filter
blocked. Check `reports/cost.jsonl` for exactly what you spent.

### 5. Retake any shot you don't like (≈ $3.33 each)

```bash
nraiorbit retake godavari5 sc002_s005 sc009_s004     # new attempt for these shots, then re-render
nraiorbit retake godavari5 sc002_s005 --use 1        # go back to an earlier attempt (free)
```

Earlier attempts are kept in `inbox/video/` (`<shot>.mp4`, `<shot>_2.mp4`, ...). The
attempt each shot uses is recorded in `projects/godavari5/takes.yaml`.

### 6. Full 5 minutes (about $121 more)

```bash
nraiorbit run godavari5
```

The trailer shots from step 4 are reused for free; only the other 37 are generated. Use
`--max-spend 200` to change the cap for one run, and `--yes` to skip the question.

### 7. Finish

- **Music:** 9 cues are planned in `08_music/cue_sheet.json`. Run `nraiorbit shotpack
  export godavari5` for a readable cue sheet (`shotpack/MUSIC.md`). Save tracks as
  `inbox/music/<cue id>.wav`, and `inbox/music/trailer.wav` for the trailer, then
  `nraiorbit run godavari5 --force render`. Until then, cues are placeholder tones.
- **Better voices:** record any line and save it as `inbox/dialogue/<line id>.wav` (the
  script is in `shotpack/DIALOGUE.md`). Recordings override Gemini TTS. Voice choices are
  `gemini_voice` in `library/voices/*/manifest.yaml`.
- **DaVinci Resolve:** import `movie.mp4`, `stems/dialogue.wav`, `stems/music.wav` and
  `subs_te.srt`. Grade the colour and do the final mix in Fairlight.
- **The real bill:** run `nraiorbit estimate godavari5`. Its "Actual spend" section reads
  `reports/cost.jsonl`. Plan the feature from that number:
  `feature ≈ actual ÷ 54 shots × ~1,400 shots`.
- **Upload (private):** run `nraiorbit publish godavari5 --dry-run` first, then without
  `--dry-run`.

### Knobs

| Setting (`projects/godavari5/pipeline.yaml`) | Effect |
|---|---|
| `google.veo_mode: keyframe` (default) | Gemini draws the first frame from the reference images; Veo animates it. 4/6/8 s clips, cheapest |
| `google.veo_mode: reference` | Veo gets the reference images directly. Clips are fixed 8 s, so more seconds are billed |
| `google.veo_model` / `veo_tier` | This project: standard (best). Fast is ~3x cheaper, Lite ~5x |
| `google.veo_resolution` | This project: 1080p (8 s clips). 720p allows 4/6/8 s clips |
| `budget.max_usd` | Hard cap for this project |
| `render.only` or `--only sc002 sc009_s004 trailer` | Generate only some scenes/shots; the rest reuse clips already paid for, or show a placeholder card if not generated yet |

### Not automated yet

- **Lip-sync.** Veo's mouth movement won't match the Telugu audio. That needs an NVIDIA
  GPU, planned for an AWS g6.xlarge at about $2 for this POC.
- **Music generation.**

## Alternative: generate clips by hand (any Veo plan, no API key)

```bash
nraiorbit poc godavari5 --manual
nraiorbit shotpack export godavari5    # SHOTPACK.md: prompt, clip length and reference images per shot
# In Veo / Flow, generate each shot; save as inbox/video/<shot_id>.mp4 (extra attempts: <shot_id>_2.mp4 ...)
nraiorbit shotpack status godavari5
nraiorbit run godavari5 --force render
```
