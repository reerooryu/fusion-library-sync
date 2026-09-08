# CLAUDE.md

Working notes for Detent. Read before changing anything.

## Rules

- The invariant, above all else: never upload a path already recorded in the
  manifest, unless the Data Panel has been checked and the file it points at
  is gone. Fusion does not enforce this and will hold three files of one name
  in one folder, each on its own lineage, silently.
- Identity is the lineage URN. Never the display name.
- Measure Fusion's API; do not assume it. Every Fusion-side bug in this project
  came from assuming. Write a probe script and read the numbers.
- Fixes carry a test that fails when the fix is reverted. Mutate the fix and
  confirm the suite catches it.
- Commits: conventional, imperative, scoped. Author and committer are
  `reerooryu <reerooryu@users.noreply.github.com>`. No other trailers.
- `CHANGELOG.md` is the single source for release notes; `scripts/publish.sh`
  reads each release body from it.

## Log

One line per commit, oldest first.

- `feat: phase 1 core` — manifest, tree/manifest diff, repo-path to Data Panel
  mapping.
- `feat: github source, data panel interface, sync orchestration` — injected
  transport, Data Panel behind a Protocol, plan/apply cycle.
- `feat: v0.1 add-in` — Fusion command, JSON config, adopt mode.
- `refactor: trim dead code and narration` — removed unreferenced code.
- `chore: rename to Detent` — entry points renamed to match the add-in folder.
- `fix: do not freeze Fusion on a first sync` — streamed the tarball in 1 MB
  chunks with a working cancel; a 2.2 GB read had blocked the UI thread.
- `fix: git-style globs, correct file count, readable preview` — replaced
  `fnmatch`; corrected the corpus count to 1,198.
- `fix: progress must say which phase it is in` — separated download and
  upload phases.
- `fix: never block on uploadState` — uploads are fired, not awaited; blocking
  cost 228 s per file.
- `fix: settle must be frugal and survive a flaky Data Panel` — one listing per
  folder per pass.
- `fix: rebuild uploads around futures, measured not assumed` — first live
  probe; `DataFolder.refresh()` does not exist.
- `test: cover the Fusion upload state machine` — upload state transitions.
- `fix: scan the folder first, poll the future only as fallback` — second
  probe; files appear at t=0 while `uploadState` takes 18–50 s.
- `fix: report altered names, and follow the library dropdown` — populated
  `Report.renamed`; bound the folder input to the selected source.
- `fix: stamp the manifest header on every sync` — `synced_at`, `ref` and
  `synced_commit` were each stale for a different reason.
- `feat: detect files the manifest claims and the Data Panel does not have` —
  drift detection, guarded against re-placing over an occupied name.
- `fix: reload the package on Run, and stop squatting on the name "core"` —
  Fusion keeps `sys.modules` across Stop/Run; package renamed `detent_core`.
- `docs: changelog for every release, and a script to publish them` —
  `CHANGELOG.md` plus `scripts/publish.sh`.
- `docs: rename v0.1 to v0.1.0 and add --retag` — tags pushed before a history
  rewrite keep the orphaned commits alive.
- `fix(sync): stop settle dropping fired uploads and claiming strangers` — two
  duplicate-producing paths in `settle`, an invented blob in
  `reconcile_inflight`, and a blocked run stamping the header. `subpath`
  rejected in config.
- `docs: changelog for v0.3.2` — release notes for the audit fixes.
- `feat: one-line installer, attached to every release` — `install.sh` plus a
  `Detent.tgz` asset built from each tag; release bodies gain a generated
  "What's Changed" list and `--print-notes` dumps them all.
- `docs: lead the README with installing and using Detent` — removed a second,
  contradictory Install section and a stale "not yet done" claim.
- `fix(github): retry transient failures and fall back from the archive` — a
  504 on the first JSON request killed a whole sync.
- `fix(ui): let fetch_files own its download progress label` — the byte
  percentage was computed and then overwritten with a frozen file counter.

## Push

```bash
cd ~/fusion-library-sync && git add -A && \
git commit -m "docs: update CLAUDE.md" && git push origin main
```
