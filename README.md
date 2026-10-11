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

Pick the one that fits you:

- **You don't have the VEX CAD library in Fusion yet.** Follow
  [Install the library](#install-the-library).
- **You already have it in Fusion.** Don't download it again. Follow
  [Use the library you have](#use-the-library-you-have).

### Install the library

1. Open **Sync Library**.
2. **Library**: VEX CAD Library
3. **Project**: the project you want it in
4. **Folder**: `VEX-CAD-Library`. It's already filled in. Detent makes the
   folder.
5. **Action**: Sync now → **OK**
6. Check the list it shows you, then click **Yes**.

The whole library is about 1,200 files and 2 GB. Detent downloads it and
hands every part to Fusion. In our tests that took about 5 minutes on a Mac
and 11 minutes on a Windows laptop, on home connections. Fusion
then uploads the parts in the background, so the done box says most of them
are `still uploading`. **Keep Fusion open until its uploads finish.** The
next time you run Sync now, Detent confirms them. Nothing is uploaded twice.

To try a small part first, see
[Sync part of the library](#sync-part-of-the-library).

### Use the library you have

You rename your library folder to `VEX-CAD-Library` and adopt it. Detent
works out which release you have, then adds whatever is newer. Your designs
keep working the whole time.

> **Do these steps in order.** If you run Sync now on your library before
> step 2, Detent sees parts it didn't put there. It won't upload copies of
> them, so nothing is duplicated, but it will list them as `NAME TAKEN`
> until you do step 2.

**Step 1. Rename the folder to `VEX-CAD-Library`.**

1. In Fusion's Data Panel, find your library folder. Inside it you should
   see the library's own folders: Electronics, Field Elements, Hardware,
   Motion and Structure.
2. Write down the folder's current name first. It often has the release in
   it: `VEX-CAD-Fusion-360-Library-2.0.5` is release `v2.0.5`. You'll only
   need it if Detent can't work the release out.
3. Right-click the folder, choose **Rename**, and type `VEX-CAD-Library`.

Renaming doesn't break your designs. They're linked to the parts
themselves, not to the folder's name. To be sure, open one of your designs
afterwards and check that it loads normally.

If those five folders sit straight in your project with no folder around
them, make a new folder called `VEX-CAD-Library` and move the five folders
into it.

**Step 2. Adopt.**

1. Open **Sync Library**.
2. **Library**: VEX CAD Library
3. **Project**: the project your library is in
4. **Folder**: `VEX-CAD-Library`
5. **Action**: Adopt existing library
6. **Installed release**: leave it blank. Detent works out your release from
   the parts in your folder. (If you know it, you can type it, for example
   `v2.0.5`.)
7. Click **OK**. Detent reads the releases on GitHub and your folders, which
   takes a few minutes, then shows a box. Check two things in it:
   - The first line names a release, for example `at v2.0.5`, and the next
     line says `Worked out from the parts in your folder`.
   - `matched` is close to `files in that release`. If it's much lower,
     check that the Folder is right.
8. Click **Yes**. This uploads nothing. It only records what you have.

If the release looks wrong, click **No** instead, and run Adopt again with
your release typed in Installed release.

**Step 3. Get what's new.**

1. Open **Sync Library**.
2. **Action**: Sync now → **OK**
3. It lists the parts released after yours. Click **Yes**.

It may also list a few parts under `Changed upstream`. Those are parts you
already have that a newer release changed. Detent leaves them alone. See
[When a release changes a part you have](#when-a-release-changes-a-part-you-have).

From now on, run Sync now whenever a new release comes out.

#### What Detent can't tell from names

Detent works out your release from which parts are in your folder. Names
can't show which version of a part you have, so two cases need you:

- **A part changed in a past release.** For example, v2.0.6 changed
  `LS Shaft` and `HS Shaft`. Detent assumes you have the older version, and
  the Adopt box says how many. The next Sync now lists them once, with how
  to update them (see
  [When a release changes a part you have](#when-a-release-changes-a-part-you-have)).
  If you installed that release in full, you can skip that by typing the
  release in Installed release.
- **It can't find any release that matches.** The box says so, and your
  parts are recorded as `unverified`. Sync now still adds new parts, but
  shows a line like `? 12 unverified` every time. Run step 2 again with
  your release typed in.

If you added parts from a newer release by hand, that's fine. Adopt records
them too, so Sync now won't try to add them again.

## Updating

**New library release:** Sync Library → Sync now.

### When a release changes a part you have

Sometimes a release changes a part you already have. v2.0.6 did this to
`LS Shaft` and `HS Shaft`: they gained a `shaft_length` parameter.

Detent never replaces a part you have. Swapping a part for a different file
breaks the designs that use it. Instead, Sync now lists those parts under
`Changed upstream`, once. After that it stops listing them.

You can keep your copy. Nothing breaks. Or update it:

1. Download the new file. You only need that one part, not the whole
   library:
   - For v2.0.6:
     [LS Shaft.f3d](https://github.com/VEX-CAD/VEX-CAD-Fusion-360-Library/raw/main/Motion/LS%20Shaft.f3d)
     and
     [HS Shaft.f3d](https://github.com/VEX-CAD/VEX-CAD-Fusion-360-Library/raw/main/Motion/HS%20Shaft.f3d).
   - For any part: open the
     [library on GitHub](https://github.com/VEX-CAD/VEX-CAD-Fusion-360-Library),
     go to the part's folder, click the file, then click
     **Download raw file** (the download arrow, top right).
2. In Fusion's Data Panel, right-click your copy of the part and choose
   **Import New Change**. Pick the file you downloaded. Fusion adds it as a
   new version of the same part, so you still have one copy, not two.
   Don't upload it as a new file next to the old one: you'd have two parts
   with the same name.
3. The next time you open a design that uses the part, Fusion says a
   reference is out of date. Click **Get Latest** on the part in the
   browser, or **Get All Latest** in the top bar. Joints are kept.

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
  Swapping in a different file would break the designs that use it. Sync
  now lists the part once instead. To update it yourself, see
  [When a release changes a part you have](#when-a-release-changes-a-part-you-have).
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
| Total time, until the parts are in Fusion | 43 min | 3 min 20 s | 2 min 35 s |
| Parts you already had, imported again | 1,173 | 0 | 0 |
| Someone has to build the zip first | no | yes | no |

- **Against the full zip, Detent takes about 94% less time.** It downloads
  95% less and gives Fusion 98% fewer parts to import. Most of the 43 minutes
  is the download: 32 minutes for the zip, then 10 for the import.
- **Detent itself is done in about 19 seconds.** The rest is Fusion
  uploading the parts in the background, as it does after any upload. You
  can keep working while it does.
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
- Detent's own part of a 33-part update (v2.0.3 to v2.0.5, 251 MB) took
  19 s: 4 s making folders, 11 s downloading, 4 s handing parts to Fusion.
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
