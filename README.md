# Lockstep

Incremental sync of a Git-hosted CAD library into Autodesk Fusion's Data Panel.

Downloads only what changed. Never touches a file it has already placed.
Refuses to guess.

**Status:** v0.1 — Fusion add-in, additive sync only. 62 tests.

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
Lockstep.py        add-in entry: Utilities > ADD-INS > Sync Library
config.json        which libraries to sync (created on first run)
core/config.py     config schema, repo-URL normalisation
core/paths.py      repo path -> Data Panel location
core/manifest.py   state: have I placed this, and which cloud file is it
core/plan.py       tree vs manifest -> add / change / orphan / unverified
core/github.py     resolve ref, read tree, fetch blobs (transport injected)
core/datapanel.py  Data Panel interface + Fusion impl + a fake that duplicates
core/sync.py       plan -> confirm -> apply, with crash recovery
core/fusion_project.py  resolving a project when activeProject throws
tests/             acceptance tests against the real corpus
```

`core/` has no Fusion dependency and runs anywhere:

```
python3 -m pytest tests/ -q
```

## Install

1. Install **GitHubToFusion360** from the Autodesk App Store (once, ever)
2. Run it, paste this repo's URL (once, ever)
3. Fusion → **Utilities → ADD-INS → Sync Library**

VEX-CAD is preconfigured, so there is no second URL to paste. Add your own
libraries by editing `config.json` beside the add-in.

## Use

**Preview changes** — lists what would move. Writes nothing.

**Sync now** — previews, asks, then uploads new files only.

**Adopt existing library** — already imported the library by hand? Adopt claims
it without uploading anything. Give the release tag you installed (`v2.0.3`)
so the next sync moves only the difference; leave it blank and the files are
recorded as unverified rather than assumed current.

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

## Testing

The fake Data Panel in `core/datapanel.py` reproduces Fusion's dangerous
behaviour on purpose: uploading a same-named file into a folder creates a
second file on a new lineage, silently. A suite that passed against a
well-behaved fake would prove nothing about the invariant.

The suite is mutation-checked. Breaking the manifest lookup, removing the
per-file flush, making inflight recovery trust the manifest instead of the
cloud, or ignoring name collisions each fail the test written to catch it.

## Not yet done

Phase 1: the add-in shell — toolbar command, dry-run preview, progress UI.
`core/` is complete and Fusion-free; only the presentation layer is missing.

Phase 2: in-place updates for changed files. Proven possible against a live
project (open the file, `BaseFeature.startEdit`, `updateBody`, `finishEdit`,
`save` — same lineage, version increments). Not built.
