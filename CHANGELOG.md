# Changelog

Use the latest release. Older releases are kept for reference and marked broken.

## v0.4.2 — Faster downloads, a folder check, and a tracking fix

- Fixed: files that were still uploading could be recorded under a temporary
  local path instead of their Fusion ID. The next Preview then listed them as
  "missing but the name is taken". Detent now waits for the real ID. Records
  made by older versions are repaired on the next Sync, without uploading
  anything.
- Downloads files 16 at a time instead of as one archive. On a home
  connection the full library took about 3 minutes instead of about 30.
- If a lot of downloads fail, for example because GitHub is limiting
  requests, Detent downloads the archive instead.
- Preview checks the folder: how many files are in it, any duplicate names,
  and any files Detent doesn't know about.
- Preview and the upload prompt show how much will be downloaded.
- The upload prompt no longer says Fusion will stop responding.
- Fusion sometimes refuses to open a folder for a moment. Detent now tries
  again. It also opens every folder before it downloads anything, so if
  Fusion still refuses, it stops in seconds and says so. Before, one refusal
  ended the whole sync, after the download.
- The note at the bottom of the dialog shows a build ID. If you update the
  add-in and don't restart Fusion, it says `OUT OF DATE`.

## v0.4.1 — Safer syncing and Windows install

**Broken.**

- Won't upload a file where one with the same name is already in the folder.
  Before, syncing into a folder that already had the library made duplicates.
- Asks before changing the Folder, since that uploads a second full copy.
  Preview no longer saves the Folder to `config.json`.
- Uploads the same GitHub version the preview showed you.
- The "Upload N files?" prompt counts every file being sent, including deleted
  files being put back. Before, putting back 45 deleted files didn't ask.
- Parts that changed upstream are listed by name.
- The progress bar is right when putting back deleted files.
- Releases include `Detent.zip` for Windows, and the README has step-by-step
  install instructions.

## v0.4.0 — Downloads only what's needed; dialog fixes

**Broken.**

- A partial sync no longer downloads the whole 2.2 GB repo.
- Files download 8 at a time.
- The progress window closes when it's done.
- Sync now is the default action.
- The tooltip no longer starts with a blank line.

## v0.3.6 — Longer upload wait for big syncs

**Broken.**

- The time allowed for uploads to finish grows with the number of files. The
  old 5-minute limit left the last file of a 1,198-file sync unconfirmed.

## v0.3.5 — Download progress that moves

**Broken.**

- The progress bar tracks bytes downloaded. It used to sit at "0/1198" for the
  whole download.

## v0.3.4 — Retry when GitHub has a hiccup

**Broken.**

- Retries timeouts and server errors up to 4 times.
- If downloading the whole repo fails, downloads the files one by one instead.

## v0.3.3 — One-line installer

**Broken.**

- Added `install.sh`. Each release includes `Detent.tgz`.

## v0.3.2 — Fixes for duplicate uploads

**Broken.**

- An upload that wasn't confirmed in time could be sent again on the next run.
- An existing file with the same name could be mistaken for Detent's upload.
- Files recovered after a crash showed as "changed" forever.
- A blocked run no longer records itself as a completed sync.
- `subpath` in config is now rejected.

## v0.3.1 — Updates take effect without restarting Fusion

**Broken.**

- Reloads its code properly on Stop and Run.
- Internal package renamed from `core` to `detent_core` so it can't clash with
  other add-ins.
- Warns if Fusion is still running an old copy of the code.

## v0.3.0 — Notices deleted files

**Broken.**

- Checks the folder against its records and re-uploads files deleted by hand.
- If a different file now has that name, reports it instead of uploading.

## v0.2.3 — Records when each sync ran

**Broken.**

- The manifest records the time, version and commit of every sync.

## v0.2.2 — Reports renamed files; Folder follows the library

**Broken.**

- Reports when a file name had to be changed to fit Fusion.
- The Folder box changes when you pick a different library.

## v0.2.1 — Faster upload confirmation

**Broken.**

- Looks in the folder for uploaded files instead of waiting on Fusion's upload
  status.

## v0.2.0 — Uploads rebuilt

**Broken.**

- Starts uploads and checks on them later, instead of waiting on each one.
- Errors are written to `state/detent.log`.

## v0.1.5 — Fewer calls to Fusion

**Broken.**

- Lists each folder once per check instead of once per file.

## v0.1.4 — Doesn't wait on each upload

**Broken.**

- Stopped waiting for each upload to finish, which made a 10-second upload take
  minutes.

## v0.1.3 — Separate download and upload progress

**Broken.**

- Progress shows whether it's downloading or uploading.

## v0.1.2 — Correct file matching

**Broken.**

- `include` patterns match the way git does. Some files were being skipped.

## v0.1.1 — Doesn't freeze on the first sync

**Broken.**

- Downloads in chunks, with a Cancel button that works.

## v0.1.0 — First release

**Broken.**

- Sync Library command, JSON config, VEX CAD preset, and Adopt.
