# Asset library

Manifests (`*/manifest.yaml`) are versioned in git. The media they reference (images,
LoRAs, embeddings, audio, mocap) belong in Git LFS / DVC / object storage and are **not**
committed here, so `nraiorbit library validate` lists them as missing until you add them.

See [docs/ASSET_LIBRARY.md](../docs/ASSET_LIBRARY.md) for the layout, manifest fields and
how the pipeline searches and pins assets.
