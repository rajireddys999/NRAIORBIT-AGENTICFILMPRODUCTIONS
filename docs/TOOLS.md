# Tools and Models Catalog

Every capability sits behind a provider interface (`src/nraiorbit/providers/base.py`), so
any row below can be swapped without touching the pipeline. Generative models improve
every few months, so **re-benchmark quarterly** on your own fixed test set: 20 shots,
20 lines and 10 cues that cover your hardest cases.

**Licences matter for a monetised YouTube channel.** "Check" means confirm the current
licence and terms before commercial use. Several popular open models have
**non-commercial weights** even when the code is MIT/Apache. Those are flagged ⚠.

---

## 1. Reasoning and language

| Purpose | Tool | Type | Notes |
|---|---|---|---|
| All creative agents (story, screenplay, casting briefs, shots, music direction, critique, vision QA) | **Claude Opus 5.5** (`claude-opus-5-5`) via Anthropic API | API | Structured outputs, prompt caching, streaming, image input for QA; SDK `anthropic` (Python) |
| High-volume cheap checks (classification, tag extraction) | Claude Haiku 5.5 (`claude-haiku-5-5`) | API | Optional, for bulk tagging of library assets |
| Telugu ↔ English translation (subtitles) | Claude; **IndicTrans2** (AI4Bharat) | API / open (MIT) | Use IndicTrans2 as a second opinion on subtitle accuracy |
| Telugu text emotion classifier | **MuRIL** (Apache-2.0), **IndicBERT v2** (MIT) fine-tuned | open | Cross-checks rasa tags |
| Transliteration / script utilities | `indic-transliteration`, Aksharamukha | open | Lexicon and name handling |

## 2. Image generation and character identity

| Purpose | Tool | Type | Licence notes |
|---|---|---|---|
| Base text-to-image / editing | **FLUX.1 / FLUX.2** family (Black Forest Labs), FLUX Kontext (reference-based editing) | open weights + API | ⚠ `[dev]` weights are non-commercial: use the BFL API/pro tier or buy a commercial licence; `[schnell]` is Apache-2.0 |
| Base text-to-image / editing (permissive) | **Qwen-Image / Qwen-Image-Edit** | open | Apache-2.0 |
| Reference-consistent image API | Gemini image models, Seedream, FLUX pro (API) | API | Good for building identity packs fast; check terms |
| Identity LoRA training | `ai-toolkit`, `kohya-ss/sd-scripts`, `diffusers` training scripts | open | Train one LoRA per character per base model |
| Face-ID adapters | **PuLID**, **InstantID**, IP-Adapter-FaceID, InfiniteYou | open | ⚠ most use InsightFace face models internally (non-commercial weights, see §9) |
| Layout / pose control | ControlNet (pose, depth, canny), **DWPose** (Apache-2.0) | open | Avoid OpenPose weights for commercial use (CMU licence) |
| Depth / segmentation | **Depth Anything V2** (Small: Apache-2.0; larger: ⚠ CC-BY-NC), **SAM 2** (Apache-2.0) | open | Masks for regional prompting and multi-character shots |
| Workflow runtime | **ComfyUI** (headless API mode) | open (GPL-3.0) | Store workflows as JSON in git; call from GPU workers |

## 3. Video generation

| Purpose | Tool | Type | Notes |
|---|---|---|---|
| Bulk image-to-video, self-hosted | **Wan 2.x** (I2V, VACE control/reference, Animate, S2V) | open (Apache-2.0) | Workhorse for most shots; supports first/last-frame and pose control; distilled "lightning" LoRAs cut cost |
| Alternative open I2V | HunyuanVideo / HunyuanVideo-I2V / HunyuanCustom | open | ⚠ Tencent community licence excludes some territories; check |
| Alternative open I2V | LTX-Video / LTX-2 (Lightricks) | open | Fast; check licence tier |
| Hero / complex shots (API) | **Kling**, **Google Veo**, **Runway** (Gen-4 references / Aleph), **OpenAI Sora 2**, Seedance, MiniMax Hailuo, Luma | API | Route the hardest shots (crowds, complex physics, close emotional acting) here; check commercial terms per provider |
| Model API aggregators | fal.ai, Replicate | API | One integration, many models; useful for A/B tests |

## 4. Motion, action and choreography

| Purpose | Tool | Type | Notes |
|---|---|---|---|
| Stock mocap | **Mixamo** | free | Royalty-free for commercial projects; good for walks and basic fights |
| Markerless mocap from phone video | Move.ai, Rokoko Video, DeepMotion | API/app | Record your own fight choreographer once and reuse forever |
| Mocap suits | Rokoko Smartsuit, Xsens | hardware | For a serious action library |
| Retarget + render pose videos | **Blender** (GPL), `bvh` tools | open | Produce `pose.mp4` skeleton videos for pose-conditioned video gen |
| Text-to-motion (fill gaps) | MoMask / T2M-GPT class models | open | check licences; good for background extras |

## 5. Lip-sync, enhancement and finishing

| Purpose | Tool | Type | Notes |
|---|---|---|---|
| Audio-driven lip-sync | **LatentSync**, MuseTalk, InfiniteTalk, Wan S2V | open | check model licences; ⚠ Wav2Lip weights are non-commercial |
| Lip-sync API | Sync Labs | API | Commercial option |
| Upscaling | **Real-ESRGAN** (BSD-3), SeedVR2, Topaz Video AI (commercial) | open/app | Upscale only final takes |
| Frame interpolation / slow-motion | **RIFE** (MIT), FILM | open | Slow-motion for elevation shots |
| Face restoration | GFPGAN (Apache-2.0); ⚠ CodeFormer (non-commercial S-Lab licence) | open | Always re-run the identity guard afterwards |
| Colour | OpenColorIO, LUTs via ffmpeg `lut3d`, DaVinci Resolve | open/app | Per-sequence LUT from production design |

## 6. Voice (Telugu)

| Purpose | Tool | Type | Notes |
|---|---|---|---|
| Open Telugu TTS | **Indic Parler-TTS** (AI4Bharat), **IndicF5**, AI4Bharat Indic-TTS | open | check licences; fine-tune on your consenting voice artists |
| Zero-shot cloning TTS | F5-TTS family | open | ⚠ base checkpoints trained on non-commercial data; use Indic/commercially licensed checkpoints or train your own |
| Commercial Telugu TTS | **Sarvam AI (Bulbul)**, ElevenLabs, Azure Neural TTS (te-IN), Google Cloud TTS (te-IN) | API | Check Telugu quality per voice; good for minor characters |
| Voice conversion | **RVC** (MIT), Seed-VC, OpenVoice v2 (MIT) | open | Path A (artist scratch read → character voice) and path C |
| Speaker verification | **SpeechBrain ECAPA-TDNN** (Apache-2.0), WeSpeaker (Apache-2.0) | open | Voice guard |
| Telugu ASR | **AI4Bharat IndicConformer**, Whisper large-v3 (MIT), Sarvam Saarika (API) | open/API | CER check vs script, subtitle timing |
| Forced alignment | torchaudio MMS forced aligner, Montreal Forced Aligner | open | Word-level subtitle timing |
| Speech emotion | emotion2vec | open | check licence |
| Speech quality | UTMOS, DNSMOS | open | MOS prediction |
| Denoise / dialogue clean-up | DeepFilterNet, Demucs (vocals) | open | For path A recordings |

## 7. Music

| Purpose | Tool | Type | Notes |
|---|---|---|---|
| Symbolic composition (raga-aware) | **music21** (BSD), **pretty_midi** (MIT), `mido` | open | Rule engine in raga scales; motif development |
| Rendering MIDI | Sample libraries (e.g. SWAR Systems Indian instruments, orchestral libraries) in **REAPER** (scriptable via ReaScript) or **FluidSynth** (LGPL) with SF2 | commercial/open | Deterministic, editable, time-locked |
| Generative music (open) | **ACE-Step** (Apache-2.0), YuE (Apache-2.0, lyrics-to-song), Stable Audio Open (community licence) | open | Fine-tune on your licensed stems for Telugu idioms. ⚠ MusicGen/AudioCraft weights are CC-BY-NC: don't use them for a monetised channel |
| Generative music (API) | ElevenLabs Music, Suno, Google Lyria | API | Check each plan's commercial-use and ownership terms |
| Stem separation | **Demucs / HTDemucs** (MIT) | open | Split reference recordings, clean stems |
| Audio → MIDI | Basic Pitch (Apache-2.0) | open | Turn musician improvisations into motif MIDI |
| Analysis: key, tempo, mood | **librosa** (ISC), Essentia (⚠ AGPL; some models non-commercial), madmom (check model licence), CREPE (MIT) | open | Music guard: raga conformance, BPM, valence/arousal |

## 8. Sound effects and ambience

| Purpose | Tool | Type | Notes |
|---|---|---|---|
| SFX libraries | Licensed packs (Sonniss GDC bundles, Soundly, Boom), Freesound (filter to CC0/CC-BY) | licensed | Avoid "personal use only" libraries |
| Text-to-SFX | ElevenLabs Sound Effects, Stable Audio Open | API/open | |
| Video-to-audio Foley | MMAudio, HunyuanVideo-Foley | open | check licences; footsteps, cloth, impacts in sync |
| Telugu crowd walla, festival ambience | Your own field recordings | owned | Generic crowds sound wrong to a Telugu ear |

## 9. Analysis models for guards

| Purpose | Tool | Licence notes |
|---|---|---|
| Face detection + embedding | **InsightFace** (RetinaFace + ArcFace `buffalo_l`/`antelopev2`) | ⚠ code MIT, **pretrained models are non-commercial research only**. Get a commercial licence from InsightFace, train ArcFace on commercially licensed data, or use a commercial face-comparison API (e.g. AWS Rekognition CompareFaces) |
| Person / costume embeddings | DINOv2 (Apache-2.0), SigLIP (Apache-2.0), OSNet re-ID | |
| Image aesthetics | LAION aesthetic predictor (MIT) | |
| Vision QA critic | Claude Opus 5.5 with image input | |

## 10. Edit, mix, master, subtitles

| Purpose | Tool | Notes |
|---|---|---|
| Rendering, concat, filters | **ffmpeg** | Used by `src/nraiorbit/post/assemble.py` |
| Timeline format | **OpenTimelineIO** (Apache-2.0) | Export to DaVinci Resolve / Premiere for human finishing |
| Programmatic editing (optional) | MoviePy | |
| Loudness | ffmpeg `loudnorm` (EBU R128), **pyloudnorm** (MIT) | Master to −14 LUFS integrated, −1 dBTP |
| Audio FX / ducking | ffmpeg `sidechaincompress`, Spotify **pedalboard** (GPL-3.0) | |
| Mastering reference | Matchering (GPL-3.0) | |
| Subtitles | SRT/VTT generation from script + forced alignment | Telugu + English tracks |
| Shot analysis | PySceneDetect | Measuring pacing of reference Telugu films |

## 11. Orchestration, infrastructure, data

| Purpose | Tool | Notes |
|---|---|---|
| Durable workflow engine | **Temporal** (MIT) or Prefect (Apache-2.0) | Each shot/take is an idempotent activity; retries survive crashes |
| Distributed GPU compute | **Ray** / Ray Serve (Apache-2.0), Kubernetes + KEDA autoscaling | One worker pool per model family |
| GPU cloud | RunPod, Lambda, CoreWeave, Modal, AWS/GCP/Azure | H100/H200/B200 for video; L40S/4090 for images, TTS, upscaling |
| Queue / cache | Redis | |
| Object storage | S3 / Cloudflare R2 / GCS / MinIO (AGPL) | Media takes and masters |
| Metadata DB | **PostgreSQL** + **pgvector** | Shots, takes, scores, costs; embeddings |
| Vector search (alt) | Qdrant (Apache-2.0) | Asset retrieval at scale |
| Asset versioning | Git LFS or **DVC** | Library binaries |
| LLM tracing | Langfuse (MIT) | Prompt versions, costs, outputs |
| Metrics / logs / errors | OpenTelemetry, Prometheus + Grafana, Sentry | Per-stage cost, latency, retake rates |
| Review UI | Next.js + FastAPI (production), Streamlit/Gradio (v1) | Gates: story, cast, pacing, retake queue, final cut |
| CI | GitHub Actions | Lint, tests, library validation on every PR |

## 12. Publishing

| Purpose | Tool | Notes |
|---|---|---|
| Upload | **YouTube Data API v3** (`videos.insert`, resumable upload), `google-api-python-client` | Set `status.containsSyntheticMedia: true` (altered/synthetic content disclosure) |
| Captions | `captions.insert` | Telugu + English SRT |
| Thumbnails | `thumbnails.set` | Generated from top-scored keyframes with title text |
| Scheduling / premieres | `status.publishAt` | Premiere the trailer first, then the film |

---

## Recommended starter stack (v1)

| Layer | Pick |
|---|---|
| Agents | Claude Opus 5.5 |
| Identity packs | Reference-based image editing API (fast, commercial) → curate → train LoRA on a commercially usable base (Qwen-Image or licensed FLUX) |
| Keyframes | ComfyUI: base model + character LoRA + face adapter + DWPose/depth ControlNet |
| Video | Wan 2.x self-hosted for ~80% of shots; one commercial API (Kling or Veo) for hero shots |
| Lip-sync | LatentSync (self-hosted) |
| Voice | Path A for leads (artist scratch + RVC), Indic Parler-TTS/IndicF5 fine-tune for the rest |
| Music | Commissioned Telugu stem library + music21 raga engine + ACE-Step fine-tune |
| SFX | Licensed pack + own Telugu crowd recordings + video-to-audio Foley |
| Guards | ArcFace (commercially licensed) + DINOv2 + ECAPA + IndicConformer + Claude vision critic |
| Orchestration | Temporal + Ray on Kubernetes, Postgres + pgvector, R2/S3 |
| Edit | ffmpeg + OpenTimelineIO export to DaVinci Resolve for the final human polish |
