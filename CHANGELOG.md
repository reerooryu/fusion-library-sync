# Changelog

Detent syncs a Git-hosted CAD library into Fusion 360's Data Panel.

**Only v0.3.3 is fit to use.** Every earlier release is left published for the
record and marked broken. Two defects affect all of them regardless of what
else they fixed:

- The Python package was called `core`. Every Fusion add-in shares one
  interpreter and one `sys.path`, so that name collides with any other add-in
  shipping the same obvious one.
- Installing an update did nothing until Fusion itself was restarted. Fusion
  re-executes the add-in's entry file on Stop/Run but keeps `sys.modules`, so
  the entry point reloaded and the package did not.

Both are fixed in v0.3.1.

The invariant the whole project exists to hold:

    Never upload a path already recorded in the manifest, unless the Data
    Panel has been checked and the file it points at is gone.

Fusion does not enforce it. It will hold three files with one name in one
folder, each on its own lineage, with no warning and no suffix.

---

## v0.3.3 — One-line installer

**The current release.** No behaviour change; v0.3.2's fixes with a way to
install them.

```bash
curl -fsSL https://raw.githubusercontent.com/reerooryu/fusion-library-sync/main/install.sh | bash
```

Unpacks the latest release into Fusion's AddIns folder and leaves
`config.json` and `state/` alone, so it upgrades an existing install as well
as creating one. It also removes a `core/` directory left by any release
before v0.3.1 — leaving it behind would let the stale package win on
`sys.path`.

Every release now carries a `Detent.tgz` built from that tag's own tree, so a
tag is installable on its own. The installer only ever fetches
`releases/latest/download`, which resolves to the newest non-prerelease, so
the releases marked broken are never what it hands anyone.

## v0.3.2 — Uploads that cannot be duplicated by a failed settle

An audit found four defects, each reproduced before it was fixed and each
mutation-checked after. Two of them could duplicate files.

- `settle()` dropped the manifest entry on three paths that run *after*
  `begin_upload` has handed the file to Fusion. The file may already be in the
  cloud; dropping the entry removes the only guard, so the next run treated
  the path as new and sent it again — a folder holding three files named
  `Part` after two syncs. The entry now stays `inflight` and
  `reconcile_inflight` settles it by looking. Only an upload that provably
  never started is dropped.
- `settle()`'s fast path claimed a lineage on a single name match, which the
  branch beside it explicitly refuses to do and which the manifest's own rule
  forbids: identity is the lineage, never the display name. A file already in
  the folder was recorded as ours while our upload sat beside it unrecorded.
  The drift scan's lineages are now passed down so `settle` knows what was
  there first — no extra listings.
- `reconcile_inflight` recorded `blob=""` — never `None`, so never
  "unverified", and never equal to a real SHA. Every file recovered from a
  crash reported as changed upstream on every sync from then on.
- A run blocked by a collision or an unmappable path uploaded nothing and then
  stamped the manifest header anyway, claiming a check it had not made.

`subpath` is now rejected in config. `paths.map_path` takes one and
`adopt_existing` passes it, but `apply_plan`, `settle`, `reconcile_inflight`
and `detect_drift` do not — so setting it made sync mirror the prefix into the
Data Panel while adopt looked for it stripped, matched nothing, wrote an empty
manifest, and duplicated the entire library on the next sync.

109 tests.

## v0.3.1 — Updates that actually take effect

**Broken.** Two settle paths can duplicate a file.

`AttributeError: 'SourceConfig' object has no attribute 'verify_placed'` — on
a field sitting plainly in the installed file. The package on disk was right;
the one in memory was not. Fusion had kept the previous version's modules.

- `Detent.py` drops the package from `sys.modules` before importing it, so
  Stop/Run is now a real reload.
- The package is renamed `core` → `detent_core`. This is load-bearing, not
  tidying: without it, the purge above would delete another add-in's cached
  modules out from under it.
- `VERSION` is recorded in both the entry point and the package and compared.
  A mismatch is reported in the dialog and again at load, instead of surfacing
  later as a missing attribute on an arbitrary field.

Anyone upgrading from an earlier version must restart Fusion once. The fix
cannot unload modules that are already cached in the running process.

103 tests.

## v0.3.0 — Notices when files are deleted behind its back

**Broken.** Ships as `core`; cannot be updated without restarting Fusion.

Empty a synced folder by hand and the previous releases reported "everything
is up to date" — a confident all-clear over an empty shelf, which is the worst
thing a sync tool can be wrong about. Only the manifest was ever consulted.

- `detect_drift` walks the Data Panel once per run and compares it against the
  manifest, matching on lineage rather than name.
- Files whose lineage is gone and whose name is free are re-placed.
- Files whose lineage is gone but whose **name is now taken** are never
  re-placed. Something else is sitting there; a second copy beside it helps
  nobody. Reported for a human instead.
- A scan that raises returns nothing rather than everything. Read the other
  way, one transient API error would re-upload an entire library on top of
  itself. A false negative costs a stale row; a false positive costs
  duplicates nobody can undo.
- `verify_placed` in the source config turns it off. Defaults on.

Verified live: a 45-file library emptied by hand reported `+0 to add / *45
MISSING`, re-placed all 45, came back holding exactly 45, and returned to
"up to date" on the next Preview.

## v0.2.3 — A manifest header that means something

**Broken.** Blind to files deleted behind its back. Plus the two defects above.

The per-file entries were always right; the header was stale three ways, each
for a different reason.

- `synced_at` was declared, serialised, read back on load, and assigned
  nowhere. It was `null` in every manifest Detent had ever written.
- `ref` was fixed at creation, because `Manifest.load(path) or Manifest(...,
  ref=src.ref)` only applies the `ref=` on the branch that builds a fresh one.
  Editing `config.json` left the manifest lying about which release it tracked.
- `synced_commit` only advanced when a file happened to be uploaded, since it
  was set below an early return taken whenever there was nothing to add. A
  library that was up to date looked identical to one never checked.

`Manifest.stamp()` now sets all three, from `sync()` rather than inside
`apply_plan`, so every non-dry-run path reaches it. Preview still writes
nothing, and a cancelled run does not claim to have checked anything.

## v0.2.2 — Altered names are reported; the dialog follows its own dropdown

**Broken.** Stale manifest header, drift blindness, and the two defects above.

- `Report.renamed` was declared, rendered by the add-in, and populated by
  nothing. `paths.py` computes an alterations list whenever it trims a name
  and the wire between them was never connected — the one case that must be
  visible, since the manifest keys on the repo path and the Data Panel keys on
  the display name.
- With more than one source configured, the Folder box stayed on the first
  library while the Library dropdown moved. Pressing OK wrote the first
  library's folder path into the second, landing both in one folder.
- Removed: `Plan.summary`, `Manifest.paths`, `Config.get`, `DataPanel.upload`
  from the Protocol (the real panel never implemented it), and an
  `except TypeError` in `fetch_files` that existed only because a test double
  had a narrower signature than the transport it stood in for.

## v0.2.1 — Scan the folder, not the future

**Broken.** Everything above, plus a manifest header that never updated.

Second live probe:

    first_appeared_s                   0.0    all five, immediately
    dataFiles_updates_without_refresh  True   the collection is live

A fired file is in `folder.dataFiles` the same tick, while `uploadState` takes
18–50 s to leave Processing. v0.2.0 had optimised the wrong direction.

- `settle` scans the folder first — one live listing per folder, no refresh.
- The future is consulted only for what the scan misses, and to disambiguate
  when two files share a name; it knows which one we created.

## v0.2.0 — Uploads rebuilt around futures, measured rather than assumed

**Broken.** Waits 18–50 s per file for information available at t=0.

First live probe against the Data Panel:

    DataFolder has no refresh(). Calling it raises AttributeError, and a bare
    'except Exception: pass' had been swallowing that on every listing since
    v0.1 — six versions of a method that never ran.

- `begin_upload` fires and returns the future; `poll_upload` resolves it.
- Every `folder.refresh()` call removed.
- Tracebacks are written to `state/detent.log`. The message box that ate the
  exception line is why five versions were spent guessing.

## v0.1.5 — Frugal settling

**Broken.** Still calls a method that does not exist, under a bare except.

`settle()` called `find_by_name` once per pending path per pass, each doing a
round trip — roughly 42 full Data Panel listings per second on a 42-file sync.
That is what was throwing. Now one listing per distinct folder per pass, with
transient errors retried rather than ending the run.

## v0.1.4 — Never block on uploadState

**Broken.** Hammers the Data Panel hard enough to make it throw.

Fusion completes an upload on the same event loop the caller is blocking, so
waiting for `uploadState` made a ~10 s upload take 300 s and time out — while
the file had already landed. Observed live: the dialog read
`Uploading 3/42 (228s)` with six files already in the Data Panel.

Estimate drops from 12 s to 2 s per file, which is what the work costs when
nobody is blocking it.

## v0.1.3 — Progress that says which phase it is in

**Broken.** A ten-second upload takes 228 seconds.

One bar served both download and upload, so finishing 43 downloads showed
"43 of 43" while zero files had been uploaded — minutes of work still to go,
looking complete and then frozen. Labels now distinguish the phases, and the
bar announces an upload before starting it rather than after finishing it.

## v0.1.2 — Git-style globs, and the real file count

**Broken.** Progress bar reports completion at the halfway point.

- Globs were `fnmatch`-based, so `*` crossed `/` and `**/` required at least
  one directory. `Hardware/Screws/**/*.f3d` silently skipped the two files
  sitting directly in that folder — 43 matched where 45 should have. Now
  compiled to a regex with git semantics.
- The corpus has **1,198** `.f3d` files, not 1,193. The original count came
  from an extension histogram that split on the last dot, so the five U+201D
  filenames landed in their own bucket and were dropped.

## v0.1.1 — Does not freeze Fusion on a first sync

**Broken.** Under-matches include globs, so syncs are quietly incomplete.

A 1,198-file sync took the tarball path and blocked Fusion's UI thread on a
single 2.2 GB read — no progress, no working cancel, and a confirm dialog that
said "upload 1198 files" while meaning "2.2 GB and about four hours". Now
streamed in 1 MB chunks with a cancel that cancels and a confirm dialog that
states bytes and time.

## v0.1.0 — First release

**Broken. Do not use.** Freezes Fusion for hours on a first sync of any real
library, with no progress and no working cancel.

Sync Library under Utilities → ADD-INS. Preview always runs before any write,
and writing needs a second explicit confirmation. JSON config, VEX-CAD
preconfigured, and an Adopt mode for a library already imported by hand.
