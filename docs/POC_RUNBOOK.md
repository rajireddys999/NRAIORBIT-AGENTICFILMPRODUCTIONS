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

## The bill

These are the numbers `nraiorbit run` prints before it spends anything (Veo 3.1 Fast,
720p, one attempt per shot):

| Run | Veo | Keyframes | Voices | **Total** |
|---|---|---|---|---|
| Character reference images (once) | — | 11 images | — | **≈ $0.44** |
| **Trailer test**: `--only trailer` (17 shots) | 114 s → $11.40 | 13 → $0.52 | 13 lines → $0.02 | **≈ $12** |
| Full 5 minutes (54 shots; trailer shots are reused) | 320 s → $32.00 | 30 → $1.20 | $0.02 | **≈ $33** |

- Retakes add cost. `nraiorbit estimate godavari5` shows the range at 1.3–3 attempts
  per shot: $42–$96 total for Veo Fast.
- Other Veo tiers: Lite is about half the Fast price; standard Veo 3.1 is 4×.
- Lip-sync on AWS, if you add it, is about $2.
- For the full 1h45m feature, the same assumptions give about $890 (Lite), $1,730 (Fast)
  or $6,770 (standard). See `nraiorbit estimate --runtime 105`.

**Spending safeguards:**
- A budget cap (`budget.max_usd`, $50 for this project) stops the run before any call
  that would cross it.
- Every paid call is logged to `reports/cost.jsonl`.
- Every Veo clip, keyframe and voice line is kept, so a re-run, crash or budget stop
  never pays twice for the same thing.

Prices are in `config/pricing.yaml`. They come from third-party trackers captured
2026-10, so verify them on Google's and AWS's own pricing pages:
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
3. If the listed names differ from `veo-3.1-fast-generate-preview`,
   `gemini-2.5-flash-image` or `gemini-2.5-flash-preview-tts`, put the right ones into
   `projects/godavari5/pipeline.yaml` under `google:`. Set `veo_tier` to match the model's
   price tier (`standard`, `fast` or `lite`).

### 3. Create the POC and build the characters

```bash
nraiorbit poc godavari5
nraiorbit estimate godavari5                 # full bill table
nraiorbit identity build godavari5           # ~$0.44; asks before spending
```

**Look at the images** in `library/characters/*/refs/` before going further. Every shot
uses these faces. To regenerate one you don't like, delete it and run `identity build`
again; only missing images are regenerated.

### 4. Trailer test (about $12)

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

### 5. Full 5 minutes (about $21 more)

```bash
nraiorbit run godavari5
```

The trailer shots from step 4 are reused for free; only the other 37 are generated. Use
`--max-spend 40` to change the cap for one run, and `--yes` to skip the question.

### 6. Finish

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
| `google.veo_model` / `veo_tier` | Lite / Fast / standard: price vs. quality |
| `budget.max_usd` | Hard cap for this project |
| `render.only` or `--only sc002 sc009_s004 trailer` | Generate only some scenes/shots; the rest get placeholder cards |

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
