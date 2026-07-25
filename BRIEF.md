# lore-web — kickoff brief

- **Problem:** Lore (the NAS VCS pilot) has no web UI anywhere — CLI-only visibility into repos.
- **Done looks like:** a read-only browser page served from the NAS: list repos, per-repo commit history, file tree at head, download a file. No auth (LAN-only, like the pilot itself).
- **Not now:** writes/commits from the browser, auth, waveform/als-diff rendering (that's the future media dashboard), pretty design.
- **First slice:** decide data path (lore-js SDK vs CLI wrapper), then repo list + history rendering end-to-end.
- **Open question:** can the SDK/CLI read remote repo metadata without a full local clone?
