# lore-web: FL archaeology — kickoff brief

- **Problem:** Years of old FruityLoops projects on the NAS (zipped project bundles
  under a `produktion` folder), with the same samples copied into every project;
  no picture of what's there, what's unique, or what the duplicates waste.
- **Done looks like:** A read-only scan report: every `.flp` parsed (pyflp) for its
  sample references, every WAV content-hashed — including inside the zips — with
  unique-vs-duplicate counts, reclaimable bytes, and a project↔sample sharing map.
- **Not now:** Touching any NAS file (no moves, hardlinks, relinks, deletes);
  Lore ingest; Reaper/video inspectors; rendering the report in the viewer UI.
  The report's numbers decide the next step.
- **First slice:** `flscan.py` — point it at the produktion FLP-zip folder; it
  walks, parses, hashes, and emits JSON plus a human-readable summary.
- **Open question:** Do the oldest (early-FruityLoops-era) `.flp` files parse with
  pyflp, and do the sample paths inside them resolve? Fallback: minimal event
  parser for just those.

*(Original repo-viewer brief delivered and superseded — see README for what shipped.)*
