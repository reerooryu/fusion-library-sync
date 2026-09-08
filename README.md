# Detent

A Fusion 360 add-in that syncs a Git-hosted CAD library into your Data Panel,
downloading only what changed.

## 1. Install Detent

Launch Fusion 360 at least once so its add-in folder exists, then run:

```bash
curl -fsSL https://raw.githubusercontent.com/reerooryu/fusion-library-sync/main/install.sh | bash
```

That unpacks the add-in into Fusion's AddIns folder. Nothing is synced yet —
this only installs the tool.

Then in Fusion:

1. **Utilities → ADD-INS → Scripts and Add-Ins**
2. **Add-Ins** tab → select **Detent** → **Run**
3. The command appears at **Utilities → ADD-INS → Sync Library**

The same command upgrades an existing install; `config.json` and `state/` are
left alone. Piping a script into a shell means trusting what it serves — the
URL above is the file itself if you would rather read it first, and every
release carries a `Detent.tgz` you can unpack by hand into
`.../API/AddIns/Detent`.

## 2. Use it

Open **Utilities → ADD-INS → Sync Library**. The dialog asks for four things:

| | |
|---|---|
| **Library** | Which configured source to sync. VEX-CAD ships preconfigured. |
| **Project** | The Fusion project to sync into. |
| **Folder** | Folder inside that project. Created if absent. |
| **Action** | Preview, Sync, or Adopt. |

**Start narrow.** A first sync of the whole VEX library is a 2.2 GB download.
Edit `config.json` beside the add-in and point `include` at one folder, confirm
it works, then widen:

```json
"include": ["Hardware/Screws/**/*.f3d"]
```

The three actions:

- **Preview changes** — lists what would move, with a size and time estimate.
  Writes nothing. Always run this first.
- **Sync now** — previews, asks, then uploads new files only.
- **Adopt existing library** — already imported the library by hand? Adopt
  claims it without uploading anything, so the next sync moves only the
  difference. Give the release tag you installed (`v2.0.3`); leave it blank and
  the files are recorded as unverified rather than assumed current.

To sync a library other than VEX-CAD, add a source to `config.json`:

```json
{
  "id": "my-lib",
  "label": "My Library",
  "repo": "owner/name",
  "ref": "v1.0.0",
  "include": ["**/*.f3d"],
  "folder_path": "My Library"
}
```

`ref` can be a branch or a release tag. Pinning to a tag is usually what you
want — a library should move when you decide it moves.

---

**Status:** v0.3.5. Additive sync, verified against a live Fusion project.
117 tests.

## Why

The reference corpus — the VEX CAD Fusion 360 library — is 1,198 files and
2.2 GB, and it barely compresses. Today every user re-downloads all of it for
every release. One release (v2.0.3) changed a single 3 KB README; everyone
still pulled 2.2 GB to get it.

Typical release churn is 10–33 files. That is the gap this closes.

## The invariant

> Never upload a path already recorded in the manifest, unless the Data Panel
> has been checked and the file it points at is gone.

Fusion accepts same-named files in one folder without warning, error, or
rename — verified against a live project: three files called `SyncProbe`, three
lineage URNs, no complaint. A duplicate upload is silent corruption the user
cannot see and cannot undo. Every structural decision here follows from
preventing that.

## Layout

```
Detent.py          add-in entry: Utilities > ADD-INS > Sync Library
install.sh         one-line installer
config.json        which libraries to sync (created on first run)
detent_core/config.py     config schema, repo-URL normalisation
detent_core/paths.py      repo path -> Data Panel location
detent_core/manifest.py   state: have I placed this, and which cloud file is it
detent_core/plan.py       tree vs manifest -> add / change / orphan / missing
detent_core/github.py     resolve ref, read tree, fetch blobs (transport injected)
detent_core/datapanel.py  Data Panel interface + Fusion impl + a fake that duplicates
detent_core/sync.py       plan -> confirm -> apply, with crash recovery
detent_core/fusion_project.py  resolving a project when activeProject throws
tests/             acceptance tests against the real corpus
```

`detent_core/` has no Fusion dependency and runs anywhere:

```bash
python3 -m pytest tests/ -q
```

The package is deliberately not called `core`: every Fusion add-in shares one
interpreter and one `sys.path`, so a generic top-level name collides with any
other add-in shipping the same obvious one.

## Design

Full spec in `docs/`. Four decisions worth knowing before reading the code:

- **Identity is the lineage URN, never the display name.** A name lookup would
  pick the wrong file, because Fusion permits duplicates.
- **Adoption runs against a named release, not HEAD.** A user on v2.0.3 whose
  files were recorded with today's hashes would look current and never update
  again.
- **Unicode is never normalised.** Five files in the corpus use U+201D
  (`0.5” OD HS Spacer`). Folding that to `"` makes the manifest key stop
  matching the tree, and every later sync sees a phantom change.
- **Uploads are fired, never awaited.** Fusion completes an upload on the same
  event loop the caller would block; waiting on `uploadState` cost 228 s per
  file for something available at t=0.

## Testing

The fake Data Panel in `detent_core/datapanel.py` reproduces Fusion's dangerous
behaviour on purpose: uploading a same-named file into a folder creates a
second file on a new lineage, silently. A suite that passed against a
well-behaved fake would prove nothing about the invariant.

The suite is mutation-checked. Breaking the manifest lookup, removing the
per-file flush, making inflight recovery trust the manifest instead of the
cloud, dropping a fired upload's manifest entry, or ignoring name collisions
each fail the test written to catch it.

## Not yet done

**Phase 2: in-place updates for files that changed upstream.** Today those are
reported and skipped. The mechanism is proven against a live project — open the
file, `BaseFeature.startEdit`, `updateBody`, `finishEdit`, `save`; same lineage,
version increments — but nothing uses it yet.

`subpath` is rejected in config until it is threaded through every call site.

## Credit

The Data Panel import approach follows
[zeulewan/fusion-batch-import](https://github.com/zeulewan/fusion-batch-import),
which proved bulk import with folder structure preserved. This project adds a
Git source, change detection, and incremental sync.
