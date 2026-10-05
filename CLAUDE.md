# CLAUDE.md

Notes for working on Detent. Read before changing anything.

## Rules

- Never upload a file the manifest says is placed, unless the Data Panel shows
  it's gone. Fusion allows two files with the same name and doesn't warn.
- Track files by lineage URN, never by name.
- Check Fusion API behaviour with a probe script before relying on it.
- A file's id is only its Fusion ID once it starts with `urn:`. While it
  uploads, Fusion reports a local file path instead.
- Fusion's data calls can fail for a moment. Retry them, and never let one
  failure end a sync or cost a download.
- Every request to Fusion is slow. Fetch `dataFolders` and `dataFiles` once
  per folder, keep what you learned for the run, and never list a folder you
  just made.
- Sync now and Preview look only at the folders they'll put files into. Only
  Check library reads everything.
- Don't make the user wait for Fusion's uploads. Hand off, wait a short fixed
  time, and confirm the rest on the next sync.
- Don't guess where time goes. Read `state/timing.log`.
- Every fix needs a test that fails without it. Revert the fix to confirm.
- Commits: conventional commits with plain, technical names. Author and
  committer are `reerooryu <reerooryu@users.noreply.github.com>`. No trailers,
  no other authors, no personal emails, no session links.
- Release notes live in `CHANGELOG.md`; `scripts/publish.sh` reads them.
- Write docs in plain language. Short sentences, no filler.
- The test plan is `testing.md` in the repo folder. Git ignores it; don't
  commit it.

## Log

One line per commit, oldest first.

- `feat(core): add manifest, plan diff and path mapping`
- `feat(core): add github source, data panel and sync orchestration`
- `feat(addin): add Fusion command with JSON config and adopt mode`
- `refactor: remove dead code and redundant comments`
- `chore: rename add-in to Detent`
- `chore: version 0.1`
- `chore: add MIT license`
- `fix(github): stream tarball download with progress and cancellation` — first sync no longer freezes Fusion.
- `fix(plan): use git-style glob semantics for include patterns` — some files were skipped.
- `fix(ui): distinguish download and upload progress phases`
- `fix(datapanel): fire uploads without blocking on uploadState` — waiting made each upload take minutes.
- `perf(sync): list each folder once per settle pass`
- `refactor(datapanel): resolve uploads through DataFileFuture`
- `test(datapanel): cover upload state transitions`
- `perf(sync): scan folders before polling upload futures` — uploaded files appear in the folder immediately.
- `fix(ui): populate renamed report and bind folder input to source`
- `fix(manifest): stamp ref, commit and timestamp on every sync`
- `feat(sync): detect manifest entries missing from the Data Panel` — files deleted by hand are re-uploaded.
- `fix(addin): reload package on run and rename core to detent_core`
- `docs: add changelog and release publishing script`
- `docs: rename v0.1 to v0.1.0 and add --retag`
- `fix(sync): stop settle dropping fired uploads and claiming strangers` — two ways to create duplicates.
- `feat: one-line installer, attached to every release`
- `chore: Update README with clearer installation steps`
- `chore: Revise installation instructions in README.md`
- `docs: lead the README with installing and using Detent`
- `fix(github): retry transient failures and fall back from the archive`
- `fix(ui): let fetch_files own its download progress label`
- `fix(sync): scale the settle deadline with the batch size`
- `perf(github): weigh the tarball decision and fetch files in parallel` — partial syncs don't download the whole repo.
- `fix(sync): block uploads onto existing names and confirm folder changes`
- `fix(release): correct v0.3.2 commit in tag map`
- `feat(release): add zip asset and Windows install steps`
- `docs(readme): describe the three actions and current limits`
- `fix(ui): include re-placed files in the upload confirmation`
- `docs: rewrite README, changelog and CLAUDE.md in plain language`
- `docs: shorten code comments and docstrings`
- `feat(sync): parallel downloads, folder check and download size` — v0.4.2.
- `fix(sync): record only real Fusion IDs, and repair old records` — uploading files report a local path as their id.
- `chore: keep the test plan out of the repo`
- `feat(ui): show the build and warn when Fusion is running old code` — two builds shared one version number, so a test ran the old one.
- `docs(readme): add time saved against the two manual update methods`
- `docs(readme): explain where synced files are stored`
- `docs(readme): use the measured full-zip and batch import times`
- `fix(sync): retry Fusion folder errors and open folders before downloading` — a full sync downloaded everything, then died on the first upload.
- `perf(sync): read only the folders a sync touches and stop waiting on uploads` — a full install took 49 min 42 s: 25 before sending, 3 sending, 22 waiting.

## Push

Run these from the repo folder.

```bash
git push origin main
```

To publish releases after pushing:

```bash
./scripts/publish.sh --go
```
