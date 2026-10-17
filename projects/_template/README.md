# A film project

Created by `nraiorbit new <slug> --story story.md` from `projects/_template`. Everything the film needs lives here as
data; the code is the `nraiorbit` framework (docs/FRAMEWORK.md).

| File or folder | What it is | Who writes it |
|---|---|---|
| `input/story.md` | the story | you |
| `pipeline.yaml` | the choices for this film (providers, budget, voice cast, per-shot settings, policy) | you; the defaults in `config/pipeline.yaml` fill the rest |
| `pricing.yaml` | price overrides (optional) | you |
| `inbox/dialogue/<line_id>.wav` | a voice take that beats generation (a recording, an approved external take) | you; a forced render reads voices from here |
| `inbox/video/<shot_id>/…`, `inbox/music/<cue_id>.wav` | clips and music made outside the pipeline | you or the providers |
| `cast.lock.yaml`, `takes.yaml`, `reuse.yaml` | casting, which take of a shot is used, clips reused from another film | the agents, then you |
| `01_…` to `11_master/` | stage outputs | the pipeline; never edit by hand |
| `reports/cost.jsonl` | the ledger, one row per paid call | the pipeline |
| `reports/qa/` | lip-sync and film QA | `nraiorbit qa` |
| `reviews/` | `<gate>.pending` / `<gate>.approved` | the pipeline and `nraiorbit approve` |

The usual order: `nraiorbit run <slug> --until shots` (plan, free except Claude), approve the story and the cast,
`nraiorbit identity build <slug>` and `nraiorbit keyframes <slug>` (faces, paid images), approve the faces,
`nraiorbit run <slug> --yes --max-spend <cap>` (picture, voices, music, lip-sync), `nraiorbit qa lipsync <slug>`,
`nraiorbit qa film <slug>`, `nraiorbit release deliver <slug>`, `nraiorbit release youtube <slug>`,
`nraiorbit backup <slug>`. Or all of it under a policy: `nraiorbit produce <slug>`.
