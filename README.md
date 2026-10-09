# Detent

A Fusion 360 add-in that keeps a CAD library from GitHub in your Fusion
project, and adds new parts when the library is updated.

It comes set up for the [VEX CAD library](https://github.com/VEX-CAD/VEX-CAD-Fusion-360-Library),
but works with any GitHub repo of Fusion files.

A new release takes three clicks and under 3 minutes, instead of 43 minutes
downloading and re-importing the whole library. See [Time saved](#time-saved).

Open Fusion 360 at least once before you install. The folder Detent goes into
doesn't exist until you do.

## Install

### Windows

1. Download **`Detent.zip`** from the
   [latest release](https://github.com/reerooryu/fusion-library-sync/releases/latest).
2. Press <kbd>Win</kbd>+<kbd>R</kbd>, paste this, and press Enter:

   ```
   %APPDATA%\Autodesk\Autodesk Fusion 360\API\AddIns
   ```

3. Open the zip and drag the **`Detent`** folder into that window.

Check that you have `AddIns\Detent\Detent.py`. If you have
`AddIns\Detent\Detent\Detent.py`, you went one folder too deep.

### Mac

Paste this into Terminal:

```bash
curl -fsSL https://raw.githubusercontent.com/reerooryu/fusion-library-sync/main/install.sh | bash
```

Or download `Detent.zip` and put the `Detent` folder in:

```
~/Library/Application Support/Autodesk/Autodesk Fusion 360/API/AddIns
```

### Turn it on

1. In Fusion: **Utilities → ADD-INS → Scripts and Add-Ins**
2. **Add-Ins** tab → select **Detent** → **Run**
3. Tick **Run on Startup**

Detent is now at **Utilities → ADD-INS → Sync Library**.

## First sync

### Recommended: a fresh copy in `VEX-CAD-Library`

Do this even if you already have the library. You don't need to know which
release you have, and Detent tracks every part from the start.

1. Open **Sync Library**.
2. **Library**: VEX CAD Library
3. **Project**: the project you want it in
4. **Folder**: `VEX-CAD-Library`. New installs fill this in for you.
   Detent makes the folder.
5. **Action**: Sync now → **OK**
6. Check the list it shows you, then click **Yes**.

The whole library is about 1,200 files and 2 GB. Detent downloads it and
hands every part to Fusion, which took about 5 minutes in our test. Fusion
then uploads the parts in the background, so the done box says most of them
are `still uploading`. **Keep Fusion open until its uploads finish.** The
next time you run Sync now, Detent confirms them. Nothing is uploaded twice.

**Already have an old copy?** Keep it. Designs that use its parts still
point to those files, and deleting it breaks them. Use `VEX-CAD-Library` for
new designs. Once nothing uses the old folder, you can delete it.

To try a small part first, see
[Sync part of the library](#sync-part-of-the-library).

### Or: keep using the copy you have

Only if you need new parts to land in the folder you already use. Otherwise,
use the fresh copy above.

1. Open **Sync Library**.
2. **Library**: VEX CAD Library
3. **Project** and **Folder**: where your library already is
4. **Action**: Adopt existing library
5. **Installed release**: the release your library came from, e.g. `v2.0.5`.
   This box appears once you pick Adopt. See below to find it.
6. **OK**. Before you click Yes, check the box that comes up:
   - The first line ends with your release, e.g. `at v2.0.5`. If it says
     `at unknown`, the Installed release box was empty.
   - `matched` is the same as `files in that release`. If it's much lower,
     check the Folder.
7. Click **Yes**.

This uploads nothing. It only records what you already have. From now on, use
**Sync now** when a new release comes out.

**Finding your release.** It's in the name of the zip you downloaded:
`VEX-CAD-Fusion-360-Library-2.0.5.zip` is `v2.0.5`. If you later added parts
from a newer release, use the newest one.

**If you leave it blank,** Adopt still works and Sync now still won't upload
those parts again. But Detent can't tell which version of each part you have,
so it marks them all `unverified`. Sync now, Preview and Verify Integrity
then show a line like `? 1198 unverified` every time, and won't tell you when
a new release changes one of those parts. Only running Adopt again with
Installed release filled in clears it.

> **Adopt first.** If you run Sync now on a folder that already has the
> library, Detent won't upload files whose names are already there. It lists
> them as `NAME TAKEN` instead. Fusion would otherwise let you have two files
> with the same name without any warning. If this happened to you, run Adopt
> now. It fixes it.

## Updating

**New library release:** Sync Library → Sync now.

**New version of Detent:** on Windows, download the zip again and drag it over
the old folder. On Mac, run the `curl` line again. Your settings are kept.

If the note at the bottom of the dialog says `OUT OF DATE` or `STALE MODULE`,
restart Fusion. Until you do, it is still running the old code.

## The four actions

| Action | What it does | When to use it |
|---|---|---|
| **Sync now** | Gets what's new. Shows you the list and asks before uploading. | To get the library, or a new release |
| **Preview changes** | Shows what Sync now would do. Changes nothing. | To see if there's an update |
| **Verify Integrity** | Reads every folder. Puts back parts that were deleted, and lists duplicate names. Slow on a big library. | If something looks wrong |
| **Adopt existing library** | Records the parts you already have, so they aren't uploaded again. Uploads nothing. | Once, if you imported the library yourself |

Pick one in the **Action** box. The dialog describes the one you've picked
right underneath.

No action changes or deletes a part you already have.

Sync now and Preview are quick because they don't read your whole library.
They compare GitHub with Detent's own record, and look only at the folders
new parts are going into.

## Verify Integrity

Sync now and Preview won't notice if you delete a part by hand. **Verify
Integrity** will. It reads every folder, which takes a while on a big
library, and offers to put back anything that's missing. It ends with a line
like this:

```
Folder check: 1198 file(s), 0 duplicate name(s), 0 not tracked by Detent
```

- **Duplicate names** are two files with the same name in one folder. Fusion
  allows this without warning. Any found are listed so you can remove one.
- **Not tracked by Detent** are files in the folder that Detent didn't place
  or adopt, such as parts you added yourself. That's fine, just good to know.

## Where the files go

Your parts are in Fusion's cloud, in the project and folder you picked.
Detent doesn't keep a copy on your computer.

During a sync, Detent downloads the new parts to a temporary folder, uploads
them into your Fusion project, and deletes the temporary folder when it
finishes. With a zip, the unzipped folder stayed on your disk. With Detent,
nothing does.

- **To change a part,** open it from the Data Panel in Fusion, edit it and
  save. Detent never overwrites a part it has placed, so your changes are
  safe from later syncs.
- **To get a part as a file,** open it in Fusion and use **File → Export**,
  or download it from the library's GitHub page.
- **Don't edit Fusion's cache.** Fusion keeps its own copies inside its
  application folder so parts open faster. Changes made there don't reach
  the cloud.

Detent keeps two things in the Detent folder, next to `Detent.py`:
`config.json` (your settings) and `state` (its record of what it placed, and
a log). Neither holds any parts.

## What it won't do

- **It won't replace parts you already have.** If a release edits an existing
  part, Detent lists it under "Changed upstream" and leaves your copy alone.
  Your robot designs link to your copy, and swapping it out can break the
  joints in those designs. If you want the new version, replace it yourself.
- **It won't delete.** Parts removed from the library stay in your project.
- **Changing the Folder after a sync uploads a second full copy** into the new
  folder. Detent asks before doing this.

## Sync part of the library

Open `config.json` in the Detent folder and change `include`:

```json
"include": ["Hardware/Screws/**/*.f3d"]
```

Set it back to `["**/*.f3d"]` for the whole library.

## Other libraries

Add another entry under `sources` in `config.json`:

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

`ref` can be a branch or a release tag. Use a tag if you want the library to
change only when you decide.

---

## Time saved

Before Detent there were two ways to get a new release of the library:

1. **Full zip.** Download the whole library as a zip and import all of it
   with Fusion Batch Import.
2. **Small zip.** Download a zip of only the new parts, then make the folders
   in Fusion and upload the parts into them by hand.

Here is each one for a real update, v2.0.4 to v2.0.5. It added 25 parts
(123 MB) in 5 new folders, to a library of 1,198 parts (2.3 GB).

| | Full zip | Small zip | Detent |
|---|---|---|---|
| Download | 2.3 GB | 123 MB | 123 MB |
| Parts Fusion has to import | 1,198 | 25 | 25 |
| Your time | 30 s | 1 min 5 s | 10 s |
| Total time | 43 min | 3 min 20 s | 2 min 35 s |
| Parts you already had, imported again | 1,173 | 0 | 0 |
| Someone has to build the zip first | no | yes | no |

- **Against the full zip, Detent takes about 94% less time.** It downloads
  95% less and gives Fusion 98% fewer parts to import. Most of the 43 minutes
  is the download: 32 minutes for the zip, then 10 for the import.
- **Against the small zip, Detent takes about 85% less of your time:** three
  clicks, instead of making five folders and filling them. The total is about
  20% less, because Fusion needs the same two minutes to import the same 25
  parts. And nobody has to build the small zip.

The small zip gets slower when a release is spread out, because the work is
per folder. 25 parts spread randomly over this library land in about 21
folders. Detent is three clicks either way.

### Where the numbers come from

Measured on one Mac on a home connection, September and October 2026:

- GitHub's zip of the whole library downloaded in 32 min 14 s.
- Fusion Batch Import put its 1,165 parts into Fusion in 10 min 17 s.
- Through Detent, Fusion imported the 25 parts of v2.0.5 in 2 min 8 s.
- Detent downloaded at 117 Mbit/s.

The full zip was timed on v2.0.3, which is a little smaller than v2.0.5
(2.0 GB, 1,165 parts). That favours the full zip.

Estimated. Every estimate favours the old ways:

- The person is very fast: 30 seconds to unzip and start a batch import, and
  10 seconds per folder to make it, open it and upload the right parts.
- The small zip downloads as fast as Detent does.
- Uploading the 25 parts by hand takes Fusion as long as it took through
  Detent.
- Detent needs 10 seconds to check GitHub and your folder before it starts.

If the full zip downloaded as fast as Detent does, it would take 13 minutes
instead of 43, and Detent would still take about 80% less time.

## How it works

Detent keeps a record, the manifest, of every file it has uploaded and which
Fusion file it became. Each sync compares GitHub with that record and with what
is actually in your folder, then uploads only what's missing.

The main rule: **it never uploads a file it has already placed**, unless it has
checked that the file is gone. Fusion allows two files with the same name in
one folder and doesn't warn you, so re-uploading by mistake creates duplicates
that are hard to spot.

Files are tracked by Fusion's internal ID, not by name.

## For developers

```
Detent.py                      the add-in: dialog and progress
detent_core/sync.py            runs a sync, recovers from crashes
detent_core/plan.py            compares GitHub with the manifest
detent_core/manifest.py        the record of what's been placed
detent_core/github.py          reads the repo and downloads files
detent_core/datapanel.py       talks to Fusion's Data Panel, plus a test fake
detent_core/paths.py           repo path to Fusion folder and file name
detent_core/config.py          config.json
detent_core/fusion_project.py  finds the Fusion project
tests/                         tests, using the real VEX file list
```

`detent_core` doesn't need Fusion, so the tests run anywhere:

```bash
python3 -m pytest tests/ -q
```

The test fake copies Fusion's duplicate behaviour on purpose, so the tests
catch duplicates.

Not done yet:

- Updating parts that changed upstream (see [What it won't do](#what-it-wont-do)).
- `subpath` in `config.json`. Use `include` instead.

`docs/phase1-spec.html` is the original design, from before the project was
renamed. Some details have changed since.

## Credit

The Data Panel import approach follows
[zeulewan/fusion-batch-import](https://github.com/zeulewan/fusion-batch-import),
which showed bulk import with the folder structure kept. Detent adds GitHub as
the source, change detection and incremental sync.

## License

MIT
