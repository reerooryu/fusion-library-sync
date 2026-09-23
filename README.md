# Detent

A Fusion 360 add-in that syncs a Git-hosted CAD library into your Data Panel,
downloading only what changed.

Run Fusion 360 at least once before installing. The folder Detent installs into
does not exist until you do.

# Step 1 — Install

## Windows

1. Download **`Detent.zip`** from the
   [latest release](https://github.com/reerooryu/fusion-library-sync/releases/latest).
2. Press <kbd>Win</kbd>+<kbd>R</kbd>, paste this, press Enter:

   ```
   %APPDATA%\Autodesk\Autodesk Fusion 360\API\AddIns
   ```

3. Open the zip. Drag the **`Detent`** folder inside it into that window.

You should end up with:

```
...\API\AddIns\Detent\Detent.py
```

If you see `AddIns\Detent\Detent\Detent.py`, you dragged one level too deep.
Delete it and drag the `Detent` folder itself.

## macOS

Paste this into Terminal:

```bash
curl -fsSL https://raw.githubusercontent.com/reerooryu/fusion-library-sync/main/install.sh | bash
```

Or do it by hand: download `Detent.zip` from the
[latest release](https://github.com/reerooryu/fusion-library-sync/releases/latest)
and put the `Detent` folder in

```
~/Library/Application Support/Autodesk/Autodesk Fusion 360/API/AddIns
```

# Step 2 — Turn it on in Fusion

1. **Utilities** → **ADD-INS** → **Scripts and Add-Ins**
2. Click the **Add-Ins** tab
3. Select **Detent** → click **Run**
4. Tick **Run on Startup** so you do not repeat this

The command now lives at **Utilities → ADD-INS → Sync Library**.

Nothing has been downloaded or synced yet. Installing only puts the tool in
place.

# Step 3 — First run: pick A or B

Read both. Pick the one that describes you.

## A — You do not have the VEX library in Fusion yet (recommended)

Also pick this if you have it but want a clean start — leave your old copy
alone and let Detent build a fresh one in its own folder.

1. **Utilities → ADD-INS → Sync Library**
2. **Library**: `VEX CAD Library`
3. **Project**: the Fusion project you want it in
4. **Folder**: type a folder name that **does not exist yet**, e.g.
   `VEX CAD Library`
5. **Action**: `Preview changes`
6. **OK** — read what it says it will do. Nothing is written.
7. Open it again, set **Action** to `Sync now`, **OK**, then **Yes**.

A first sync of the whole library is a 2.2 GB download and about 1,200
uploads. It takes a long while, and Fusion is unresponsive during the download
part. To test it on something small first, see
[Sync less than the whole library](#sync-less-than-the-whole-library).

## B — You already have the library and want to keep it

Adopt tells Detent which files you already have, so it uploads nothing now and
only moves the difference on future releases.

1. **Utilities → ADD-INS → Sync Library**
2. **Library**: `VEX CAD Library`
3. **Project** and **Folder**: the project and folder your library is
   **already in**
4. **Action**: `Adopt existing library`
5. **Installed release**: the version you currently have, e.g. `v2.0.5`.
   Leave blank if you do not know — those files are then marked unverified
   rather than assumed current.
6. **OK** → check the matched count looks right → **Yes**

From then on, use **Sync now** for every future release.

> **Don't point `Sync now` at a folder that already has the library in it
> without adopting first.** Detent will refuse those files and list them as
> `NAME TAKEN`. That is it working, not failing. Fusion lets you put two files
> with the same name in one folder and says nothing, and you usually don't
> notice until a part stops updating weeks later.

# Updating

**Detent itself** — Windows: download `Detent.zip` again and drag it over the
top, replacing files. macOS: re-run the `curl` line. Your `config.json` and
`state/` are not in the archive, so your settings survive either way.

**The library** — **Utilities → ADD-INS → Sync Library**, **Action**:
`Sync now`. Detent works out what changed and uploads only that.

If the dialog ever says `STALE MODULE`, restart Fusion. That means Fusion
served a cached copy of the old version.

# What the three actions are for

**Preview changes** tells you what would happen. It changes nothing.

**Sync now** adds the files you are missing. It runs a preview first and shows
it to you, then asks before uploading anything.

**Adopt existing library** tells Detent which files you already have. It
uploads nothing. You do this once, not every time.

Pick by what you actually want:

| What you want | Action |
|---|---|
| I already have the library and want Detent to take it from here | Adopt, once |
| I want the library and don't have it | Sync now |
| A new release came out and I want it | Sync now |
| Has anything changed in the repo? | Preview changes |
| Did someone delete files from my library? | Preview changes |
| How big is this going to be before I commit to it? | Preview changes |

You don't need to run Preview before every sync. Sync now already does one and
shows it to you. Preview is for when you only want to look.

# What Detent does not do yet

**It adds files. It does not update files that changed.**

If a release changes a file you already have, Detent lists it as "changed
upstream" and leaves it alone. You keep the old version. To get the new one,
download that file from the repo and replace it yourself.

So a release that only adds parts syncs completely. A release that also edits
existing parts syncs the new ones and skips the edits. The preview tells you
which is which before you start.

It also cannot delete. If a file is removed from the repo, Detent says so and
leaves your copy alone.

# The dialog, field by field

| | |
|---|---|
| **Library** | Which library to sync. VEX-CAD is already set up. |
| **Project** | The Fusion project to put it in. |
| **Folder** | Folder inside that project. Created if it doesn't exist. |
| **Action** | Preview changes, Sync now, or Adopt existing library. |
| **Installed release** | Only used by Adopt. Ignored the rest of the time. |

If you change **Folder** after a sync, Detent sees an empty folder and uploads
a fresh copy of everything into it. Your old copy stays where it was, because
Detent can't delete it. It warns you and asks first.

## Sync less than the whole library

Edit `config.json` next to the add-in and narrow `include`:

```json
"include": ["Hardware/Screws/**/*.f3d"]
```

Confirm that works, then widen it back to `["**/*.f3d"]`.

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

**Status:** v0.4.1. Additive sync, verified against a live Fusion project.
160 tests.

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
rename, fully verified. A duplicate upload is silent corruption the user
cannot undo. Every structural decision here follows from
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

**Updating a file that changed upstream.** Detent reports these and skips them.
Nothing in the code can update a file yet.

The Fusion side of it has been checked and works: open the document,
`BaseFeature.startEdit`, `updateBody`, `finishEdit`, `save` — the file keeps
its lineage and the version number goes up. That was a one-off probe inside
Fusion to confirm the API behaves. It has not been built into the add-in.

What is still needed: fetching the new version, matching it to the existing
document, doing the swap, recording the new blob, and deciding what to do when
the user has edited that file themselves or the swap fails partway through.

`subpath` is rejected in config until it is threaded through every call site.

## Credit

The Data Panel import approach follows
[zeulewan/fusion-batch-import](https://github.com/zeulewan/fusion-batch-import),
which proved bulk import with folder structure preserved. This project adds a
Git source, change detection, and incremental sync.
