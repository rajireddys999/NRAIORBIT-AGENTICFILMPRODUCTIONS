# 5-minute POC runbook: "Godavari Simham"

Goal: produce a real 5-minute Telugu short end to end on your own setup (MacBook Pro 48 GB,
Google Veo, DaVinci Resolve, AWS, Claude), and measure what it actually costs.

The story, screenplay (10 scenes, 13 Telugu lines), shot plan (54 shots, each ≤ 8 s for
Veo) and music cue sheet are already written. They were written in Claude Code on your
subscription, so the pipeline makes **no Claude API calls** for this POC.

## The bill, before you start

`nraiorbit estimate godavari5` prints this from the real shot plan. Here are the
expected-case numbers (2 attempts per shot on average):

| Video option | Video | Everything else | **Total** |
|---|---|---|---|
| Veo 3.1 Lite, API ($0.05/s) | $32 | $2 | **≈ $34** |
| Veo 3.1 Fast, API ($0.10/s) | $64 | $2 | **≈ $66** |
| Veo 3.1, API ($0.40/s) | $256 | $2 | **≈ $258** |
| Google AI Pro subscription, Veo 3.1 Fast (20 credits/clip) | 2,160 credits ≈ 2.2 months of AI Pro | $2 | **≈ $62** |

- One attempt at every shot = 54 Veo clips = 320 billed seconds; shots round up to 4/6/8 s
  clips.
- Range: 1.3 attempts per shot (low) to 3 (high). The Fast tier ranges from $42 to $96.
- "Everything else" is lip-sync on one AWS g6.xlarge for about 2 hours (≈ $2) plus S3
  storage (cents).
- Your Mac does the keyframes, Telugu voices, checks and edit at no cost: about 2.5–11
  hours of machine time.
- For comparison, the same estimate for a full 1h45m film (`nraiorbit estimate
  --runtime 105`) is about **$890 (Lite) / $1,730 (Fast) / $6,770 (standard)** in Veo
  API fees, plus about $50 for everything else.

Prices are in `config/pricing.yaml`. They come from third-party price trackers, captured
2026-10, so verify them on Google's and AWS's own pricing pages before you budget:
[Veo API per-second prices](https://benchlm.ai/media-pricing/veo),
[Flow credits per plan](https://www.hooked.so/compare/veo-3-pricing),
[credits per clip](https://diyai.io/ai-tools/video-generation/google-veo-pricing/),
[AWS g6.xlarge, Mumbai](https://www.doit.com/compute/spot/ap-south-1/g6.xlarge),
[AWS g5.xlarge, Mumbai](https://www.doit.com/compute/spot/ap-south-1/g5.xlarge).

**To keep the bill small:** generate with Veo 3.1 Lite or Fast at 720p (the pipeline
upscales to 1080p). Get the reference images right first (step 3): most retakes come from
faces that don't match.

## Steps

### 1. Install on your Mac

```bash
brew install ffmpeg python@3.12
git clone -b claude/telugu-ai-movie-generator-yhb6po https://github.com/rajireddys999/NRAIORBIT-AGENTICFILMPRODUCTIONS.git
cd NRAIORBIT-AGENTICFILMPRODUCTIONS
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
nraiorbit demo godavari     # 30-second sanity check: placeholder movie + trailer
```

### 2. Create the POC project and check the bill

```bash
nraiorbit poc godavari5
nraiorbit estimate godavari5          # saved to projects/godavari5/reports/cost_estimate.md
```

### 3. Make the character reference images (consistent faces)

Veo keeps a face consistent only if you give it the same reference images every time.
For Arjun, Meera and Bhairava:

1. Generate one portrait you like per character. Use an image model on your Mac (ComfyUI
   with FLUX/Qwen-Image) or Gemini's image generation, and the description in
   `projects/godavari5/01_story_bible.json`. The faces must be fictional.
2. From that portrait, make: a front face, a three-quarter face, and a full-body shot in
   each costume (`cst_arjun_village`, `cst_arjun_climax`, `cst_meera_saree`,
   `cst_bhairava_white`).
3. Save them into the library:
   ```
   library/characters/arjun_hero/refs/face/01_front.png, 02_three_quarter.png
   library/characters/arjun_hero/refs/costumes/cst_arjun_village/01.png
   library/characters/arjun_hero/refs/costumes/cst_arjun_climax/01.png
   library/characters/meera_heroine/refs/face/...      refs/costumes/cst_meera_saree/...
   library/characters/bhairava_villain/refs/face/...   refs/costumes/cst_bhairava_white/...
   ```

### 4. Export the shot pack

```bash
nraiorbit shotpack export godavari5
```

This writes `projects/godavari5/shotpack/`:

| File | What it is |
|---|---|
| `SHOTPACK.md` | Every shot in order: the Veo prompt, clip length, and which reference images to attach |
| `<shot_id>/prompt.txt` + `<shot_id>/refs/` | The same per shot, with the reference images copied in |
| `DIALOGUE.md` | Recording script: line id, speaker, Telugu text, English, delivery notes |
| `MUSIC.md` | Cue sheet: start, length, emotion, raga/tala/BPM, instruments, theme |
| `shots.jsonl` | Machine-readable version, for automating Veo later |

### 5. Generate the video clips in Veo

For each shot in `SHOTPACK.md`:

1. Paste the prompt and the negative prompt. Set the clip length (4/6/8 s) and 16:9.
2. Attach the shot's reference images. In Flow this is "Ingredients to video"; in the API
   it's reference images.
3. Save the clip you like as `projects/godavari5/inbox/video/<shot_id>.mp4`.
4. **Keep the other attempts too**, as `<shot_id>_2.mp4` and `<shot_id>_3.mp4`, so the
   bill shows what you really spent.

Check progress with `nraiorbit shotpack status godavari5`. Shots without clips show a
labelled "MISSING" card in the film, so you can assemble at any point.

### 6. Voices (Telugu)

Choose one of these, or mix them per line:

- **Record:** a Telugu voice artist reads `DIALOGUE.md`. Save each line as
  `inbox/dialogue/<line id>.wav` (mp3/m4a also work).
- **Telugu TTS on your Mac** (free; Indic Parler-TTS):
  ```bash
  pip install torch soundfile "git+https://github.com/huggingface/parler-tts.git"
  ```
  Then in `projects/godavari5/pipeline.yaml`, set `voice: {inbox_fallback: indic_parler}`.
  Any line without a recording is then spoken by the TTS. This adapter was written from
  the model card and hasn't been run yet, so tell me if it errors.

### 7. Music

Save cue audio as `inbox/music/<cue id>.wav`, following `MUSIC.md` (9 cues). Cues are
trimmed or padded and faded to fit. Missing cues play a placeholder tone. For the POC you
can also leave music out and score it in Resolve.

### 8. Assemble

```bash
nraiorbit run godavari5 --force render
```

Outputs in `projects/godavari5/11_master/`:
- `movie.mp4`: 1080p, −14 LUFS
- `stems/dialogue.wav` and `stems/music.wav`
- `subs_te.srt` and `subs_en.srt`

Re-run the same command whenever you add more clips, voices or music. Your inbox files are
never deleted.

Approve the cut to get the trailer:

```bash
nraiorbit approve godavari5 final_cut && nraiorbit run godavari5
nraiorbit approve godavari5 trailer
```

### 9. Finish in DaVinci Resolve

Import `movie.mp4`, the two stems and the subtitle files. Do the colour grade, mix
dialogue and music in Fairlight, and fix any cuts. The pipeline did the timing; Resolve
is for polish.

### 10. Lip-sync (optional for the POC)

Veo's mouth movements won't match the Telugu audio. Lip-sync needs an NVIDIA GPU. Plan:
an AWS g6.xlarge with LatentSync, about 2 hours (≈ $2). The adapter isn't built yet: it's
the next step once the POC clips exist. Without it, the POC still shows whether faces,
pacing, voices and the bill work.

### 11. Compare actual vs. estimate

```bash
nraiorbit estimate godavari5
```

The "Actual so far" section counts every clip in `inbox/video/` (kept or not) and prices
it at each Veo tier. Use that number, not the estimate, to plan the full film:

```
feature cost ≈ (actual Veo $ for the POC ÷ 54 shots) × ~1,400 shots
```

### 12. Upload (private)

```bash
nraiorbit publish godavari5 --dry-run      # check title, chapters, AI disclosure
nraiorbit publish godavari5 --client-secrets client_secret.json   # uploads as private
```
