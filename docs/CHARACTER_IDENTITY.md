# Character Identity Guard: Same Face and Body for the Whole Film

Identity drift is the most visible failure in AI films: the hero's jawline, skin tone,
hair or build changes between shots. The pipeline attacks it in three layers:

1. **Lock:** build a canonical *identity pack* once and pin it in `cast.lock.yaml`.
2. **Condition:** every image and video generation is conditioned on that pack.
3. **Verify:** every generated frame is measured against the pack, and anything that
   drifts is retaken or repaired automatically.

---

## 1. The identity pack (built once per character)

Stored at `library/characters/<id>/` (see [ASSET_LIBRARY.md](ASSET_LIBRARY.md)):

| Item | Contents | Used for |
|---|---|---|
| `refs/face/` | 12–20 face images: front, ¾ left/right, profile left/right, up/down tilt; neutral + 8 expressions (one per navarasa) | identity adapter refs, embedding centroid |
| `refs/body/` | Full-body front/side/back in a neutral A-pose, plus 2–3 action poses | body re-ID, pose conditioning, video model references |
| `refs/costumes/<costume_id>/` | Full-body + detail shots per costume (default, song, fight, climax-torn …) | costume continuity, reference images |
| `lora/` | Identity LoRA trained on 20–40 curated pack images (for the image model and, where supported, the video model) | strongest identity conditioning |
| `embeddings/face_centroid.npy` | Mean of L2-normalised ArcFace embeddings over the face refs | identity guard |
| `embeddings/body_centroid.npy` | Mean DINOv2/SigLIP embedding of person crops per costume | body/costume guard |
| `character.yaml` (manifest) | Physical spec: height (cm), build, skin tone (LAB range), hair, eye colour, marks/tattoos, age; per-character guard thresholds | prompts, guard thresholds |

### How a pack is created

```mermaid
flowchart LR
    S["Character spec<br/>(from Story Bible)"] --> B["Designer agent writes<br/>visual brief + prompts"]
    B --> G["Generate 50–100 candidates<br/>(image model, fixed seed sweep)"]
    G --> H["Human picks the face<br/>(gate: cast)"]
    H --> E["Expand: same face, all angles,<br/>expressions, costumes<br/>(reference-based editing)"]
    E --> F["Filter: keep images with<br/>ArcFace sim ≥ 0.70 to the chosen face"]
    F --> T["Train identity LoRA"]
    T --> V["Validate: 30 test prompts,<br/>measure identity scores"]
    V --> R["Register in library<br/>+ compute centroids"]
```

**Use synthetic faces, or real actors with signed consent.** Never reproduce a real
celebrity's likeness. If you cast real people, store the consent document reference in
the manifest (`consent.document_ref`).

---

## 2. Conditioning at generation time

| Stage | Technique | Notes |
|---|---|---|
| Keyframe image | Identity LoRA + face-ID adapter (PuLID / InstantID / IP-Adapter-FaceID / InfiniteYou) + reference image of the costume | LoRA for overall look, adapter for exact facial features |
| Keyframe layout | ControlNet / pose (DWPose skeleton from the action library), depth for location layout | Keeps body proportions and blocking stable |
| Multi-character shots | Regional prompting or masks per character, one LoRA/adapter per region | Prevents **identity bleed** (two characters merging into one face) |
| Image → video | Use the approved keyframe as the first frame, optionally an end frame; models with subject/reference input (e.g. Wan VACE/Phantom-style reference conditioning, Kling elements, Runway references, Veo reference images) | Shorter clips drift less: generate 5 s and extend rather than one 15 s take |
| Action | Pose-sequence conditioning from mocap (Wan Animate / VACE-style control) | Keeps the body the same during fast motion |
| Lip-sync / restore | Run identity guard **after** these passes | Face restoration can quietly change identity |

---

## 3. Verification: the guard

Implemented in `src/nraiorbit/guards/identity.py`.

### 3.1 Per frame

1. Sample frames from the clip (default: 1 every 6 frames, plus first and last).
2. Detect faces (RetinaFace via InsightFace) and compute L2-normalised ArcFace embeddings.
3. Build the similarity matrix **faces × expected characters** (cosine similarity to each
   character's face centroid).
4. Assign faces to characters with an optimal one-to-one matching. Unmatched faces count
   as *unknown faces* (extras are fine if the shot allows background people).
5. For each expected character, record the best similarity, or *missing* if the
   character is not detected and the shot size implies a visible face.

### 3.2 Per clip

| Check | Default threshold | Failure meaning |
|---|---|---|
| Mean face similarity | ≥ 0.55 (CU/MCU), ≥ 0.45 (MS), skipped for WS/EWS with face < 48 px | wrong face |
| Minimum face similarity | ≥ 0.40 | identity broke for some frames |
| Similarity std-dev across frames | ≤ 0.08 | identity **drift** within the clip |
| Identity swap | character A's face matches B better than A | bleed/swap between characters |
| Body/costume similarity | ≥ 0.75 (DINOv2 cosine, person crop vs costume centroid) | wrong costume / body shape |
| Skin tone | ΔE2000 ≤ 8 vs manifest LAB range (face skin region) | lighting or identity shift |
| Height ratio (two-shots) | within ±10 % of manifest ratio | body proportions changed |

These numbers are **starting points**. Generated faces score lower than real photos on
ArcFace, so calibrate per character: run 50 known-good and 50 known-bad takes, then set
the threshold at the equal-error point. The guard logs every score to
`reports/guards.jsonl` for this.

### 3.3 What happens on failure

```
fail ─► retake 1: new seed
     ─► retake 2: raise identity adapter / LoRA weight, add more face refs
     ─► retake 3: shorter clip, simpler camera move, or a different provider
     ─► repair pass: identity-preserving face refinement on drifting frames using
                     the character's own pack (never someone else's face)
     ─► human queue: accept / fix / re-plan the shot
```

---

## 4. Continuity guard (beyond the face)

Implemented as rules plus a vision critic (Claude with image input), comparing the shot to
the scene's production design:

- costume ID for each character in this scene (from the lockfile + production design),
- props in hand (sword, phone, bike) carried between consecutive shots,
- time of day and weather (lighting direction and colour temperature),
- wounds, dirt and torn clothes that must **persist** after a fight beat,
- screen direction (180° rule) between cuts in dialogue and chase scenes.

A failed continuity check produces the same retake signal as an identity failure.
