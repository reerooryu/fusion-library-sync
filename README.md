# Detent

A Fusion 360 add-in that installs a CAD library from GitHub into your Fusion
project, then adds new parts when the library updates.

Set up for the [VEX CAD library](https://github.com/VEX-CAD/VEX-CAD-Fusion-360-Library).
Works with any GitHub repo of Fusion files.

## Install

Open Fusion 360 once before installing.

**Windows**

1. Download **`Detent.zip`** from the
   [latest release](https://github.com/reerooryu/fusion-library-sync/releases/latest).
2. Press <kbd>Win</kbd>+<kbd>R</kbd>, paste this, press Enter:

   ```
   %APPDATA%\Autodesk\Autodesk Fusion 360\API\AddIns
   ```

3. Drag the **`Detent`** folder from the zip into that window. You should
   end up with `AddIns\Detent\Detent.py`.

**Mac:** paste this into Terminal:

```bash
curl -fsSL https://raw.githubusercontent.com/reerooryu/fusion-library-sync/main/install.sh | bash
```

**Turn it on**

1. In Fusion: **Utilities → ADD-INS → Scripts and Add-Ins**.
2. **Add-Ins** tab → **Detent** → **Run**. Tick **Run on Startup**.

Detent is now at **Utilities → ADD-INS → Sync Library**.

## First sync

### New to the library

1. Open **Sync Library**.
2. **Project**: where you want the library. **Folder**: `VEX-CAD-Library`.
3. **Action**: Sync now → **OK** → **Yes**.

That's about 1,200 parts. Keep Fusion open until its uploads finish, then
run Sync now once more to confirm them.

### Already have the library

1. In Fusion's Data Panel, rename your library folder (the one with
   Electronics, Field Elements, Hardware, Motion and Structure inside) to
   `VEX-CAD-Library`. Your designs keep working.
2. Open **Sync Library**. **Project**: the one your library is in.
   **Folder**: `VEX-CAD-Library`.
3. **Action**: Adopt existing library. Leave **Installed release** blank.
   **OK**.
4. The box names your release, for example `at v2.0.5`. Click **Yes**.
   Nothing is uploaded.
5. **Action**: Sync now → **OK** → **Yes**.

Adopt before your first Sync now.

If those five folders sit directly in your project, make a folder called
`VEX-CAD-Library` and move them into it first.

## Updating

**New library release:** Sync Library → Sync now → **Yes**.

**New Detent version:** Windows: download the zip again and drag it over the
old folder. Mac: run the `curl` line again. Then restart Fusion.

### When a release changes a part you have

Sync now lists it once under `Changed upstream` and leaves your copy as it
is. Keeping it is fine. To update it:

1. Download the new file:
   - v2.0.6:
     [LS Shaft.f3d](https://github.com/VEX-CAD/VEX-CAD-Fusion-360-Library/raw/main/Motion/LS%20Shaft.f3d),
     [HS Shaft.f3d](https://github.com/VEX-CAD/VEX-CAD-Fusion-360-Library/raw/main/Motion/HS%20Shaft.f3d)
   - Any part: open it in the
     [library on GitHub](https://github.com/VEX-CAD/VEX-CAD-Fusion-360-Library)
     → **Download raw file**.
2. In the Data Panel, right-click your copy → **Import New Change** → pick
   the file.
3. In designs that use it, click **Get Latest**.

## The four actions

| Action | What it does |
|---|---|
| **Sync now** | Adds what's new. Shows the list and asks first. |
| **Preview changes** | Shows what Sync now would do. Changes nothing. |
| **Verify Integrity** | Checks every folder and puts back deleted parts. Slow. |
| **Adopt existing library** | Records a library you already have. Uploads nothing. |

## Good to know

- Detent never replaces or deletes a part you have.
- Changing **Folder** after a sync uploads a second copy into the new
  folder. Detent asks first.
- `? N unverified`: run Adopt again with your release typed in.
- `OUT OF DATE` at the bottom of the dialog: restart Fusion.
- Each computer keeps its own record. On a second computer, run Adopt.
- Parts live in your Fusion project. Detent keeps no copies. Its settings
  are in `config.json` in the Detent folder.

### Only part of the library

In `config.json`, change `include`:

```json
"include": ["Hardware/Screws/**/*.f3d"]
```

`["**/*.f3d"]` is the whole library.

### Another library

Add an entry under `sources` in `config.json`:

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

`ref` is a branch or a release tag.

## For developers

Detent records every part it places and the Fusion file it became, and never
uploads a part it has already placed. The logic is in `detent_core/` and
doesn't need Fusion; `Detent.py` is the add-in.

```bash
python3 -m pytest tests/ -q
```

## Credit

The Data Panel import approach follows
[zeulewan/fusion-batch-import](https://github.com/zeulewan/fusion-batch-import),
which showed bulk import with the folder structure kept. Detent adds GitHub as
the source, change detection and incremental sync.

## License

MIT
