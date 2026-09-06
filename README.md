# Lockstep

Incremental sync of a Git-hosted CAD library into Autodesk Fusion's Data Panel.

Downloads only what changed. Never touches a file it has already placed.
Refuses to guess.

**Status:** Phase 1 core logic, tested. No Fusion integration yet.

## Why

The reference corpus — the VEX CAD Fusion 360 library — is 1,193 files and
2.2 GB, and it barely compresses. Today every user re-downloads all of it for
every release. One release (v2.0.3) changed a single 3 KB README; everyone
still pulled 2.2 GB to get it.

Typical release churn is 10–33 files. That is the gap this closes.

## The invariant

> Never call `uploadFile` on a path already recorded in the manifest.

Fusion accepts same-named files in one folder without warning, error, or
rename — verified against a live project: three files called `SyncProbe`, three
lineage URNs, no complaint. A duplicate upload is silent corruption the user
cannot see and cannot undo. Every structural decision here follows from
preventing that.

## Layout

```
core/paths.py      repo path -> Data Panel location
core/manifest.py   state: have I placed this, and which cloud file is it
core/plan.py       tree vs manifest -> add / change / orphan / unverified
tests/             acceptance tests against the real corpus
```

`core/` has no Fusion dependency and runs anywhere:

```
python3 -m pytest tests/ -q
```

## Design

Full spec, including adopt mode and acceptance criteria, in `docs/`.

Three decisions worth knowing before reading the code:

- **Identity is the lineage URN, never the display name.** Name lookup would
  pick the wrong file, because Fusion permits duplicates.
- **Adoption runs against a named release, not HEAD.** A user on v2.0.3 whose
  files were recorded with today's hashes would look current and never update
  again.
- **Unicode is never normalised.** Five files in the corpus use U+201D
  (`0.5” OD HS Spacer`). Folding that to `"` makes the manifest key stop
  matching the tree, and every later sync sees a phantom change.

## Credit

The Data Panel import approach follows
[zeulewan/fusion-batch-import](https://github.com/zeulewan/fusion-batch-import),
which proved bulk import with folder structure preserved. This project adds a
Git source, change detection, and incremental sync.

## Not yet done

Phase 1: Fusion add-in shell, folder creation, upload loop, dry-run UI.
Phase 2: in-place updates for changed files — proven possible, not built.
