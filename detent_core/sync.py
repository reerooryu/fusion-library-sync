"""Sync: plan, confirm, apply.

Only new files are uploaded. Everything else is reported.

Rule: never upload a path the manifest already has. Fusion allows duplicate
names, so the manifest is the only guard. It is saved before each upload
(inflight), after it's confirmed (placed), and after every file.
"""

from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple
import shutil
import tempfile
import time

from . import github as gh
from . import paths as P
from . import plan as PL
from .datapanel import DataPanel, UploadFailed, is_fusion_id
from .manifest import Manifest, ADOPTED, INFLIGHT, PLACED, adopt


Progress = Callable[[int, int, str], None]

# How long to wait for Fusion to confirm uploads before handing back. Zero:
# don't wait. Fusion uploads in the background and the next sync confirms.
# Waiting made a 19-second update take 2 minutes with nothing confirmed
# (7 Oct 2026), and a full library 20 minutes (5 Oct 2026).
CONFIRM_WAIT = 0.0


class Clock:
    """Seconds spent in each part of a run, in the order they happened."""

    def __init__(self):
        self.parts: Dict[str, float] = {}
        self._at = time.time()

    def lap(self, name: str) -> None:
        now = time.time()
        self.parts[name] = self.parts.get(name, 0.0) + (now - self._at)
        self._at = now


def timing_text(parts: Dict[str, float]) -> str:
    """'3 min 12 s (download 2 min 31 s, send 20 s)'. Parts under a second
    are left out."""
    def span(seconds: float) -> str:
        whole = int(round(seconds))
        return f"{whole // 60} min {whole % 60} s" if whole >= 60 else f"{whole} s"
    shown = [f"{name} {span(t)}" for name, t in parts.items() if t >= 1.0]
    total = span(sum(parts.values()))
    return f"{total} ({', '.join(shown)})" if shown else total


@dataclass
class Report:
    added: List[str] = field(default_factory=list)
    replaced: List[str] = field(default_factory=list)   # subset of added: were missing
    skipped_changed: List[str] = field(default_factory=list)
    skipped_orphan: List[str] = field(default_factory=list)
    unverified: List[str] = field(default_factory=list)
    failures: List[Tuple[str, str]] = field(default_factory=list)
    renamed: List[Tuple[str, str]] = field(default_factory=list)
    reconciled: List[Tuple[str, str]] = field(default_factory=list)
    collisions: Dict[str, List[str]] = field(default_factory=dict)
    unmappable: List[Tuple[str, str]] = field(default_factory=list)
    conflicts: List[Tuple[str, str]] = field(default_factory=list)
    # New files whose name is already taken in the folder. Not uploaded.
    occupied: List[Tuple[str, str]] = field(default_factory=list)
    # Bytes this run will download. Excludes files refused above.
    download_bytes: int = 0
    # The folder as it was when the run started. None if it wasn't scanned.
    check: Optional["Drift"] = None
    # Fusion wouldn't open a folder, so the run stopped before downloading.
    stopped_early: bool = False
    # Sent, but Fusion hadn't confirmed them when the wait ended. Not a
    # failure: Fusion is still uploading them, and the next sync checks.
    pending: List[str] = field(default_factory=list)
    # Seconds per part of the run. See Clock.
    timings: Dict[str, float] = field(default_factory=dict)
    # Folders the new files go into. Used for the time estimate.
    target_folders: int = 0

    @property
    def ok(self) -> bool:
        return not (self.failures or self.collisions
                    or self.unmappable or self.conflicts or self.occupied)

    def lines(self) -> List[str]:
        rows = [
            # Everything handed to Fusion, confirmed or not.
            ("added", self.added + self.pending, ""),
            ("re-placed", self.replaced, "(were gone from the Data Panel)"),
            ("changed upstream", self.skipped_changed, "(not replaced)"),
            ("gone upstream", self.skipped_orphan, "(cannot delete)"),
            ("unverified", self.unverified, ""),
            ("still uploading", self.pending,
             "of those (confirmed on the next sync)"),
            ("reconciled", self.reconciled, ""),
            ("renamed", self.renamed, "(see detail)"),
            ("NAME TAKEN", self.occupied, "(not uploaded)"),
            ("CONFLICTS", self.conflicts, "(needs a human)"),
            ("failed", self.failures, ""),
            ("COLLISIONS", self.collisions, "(blocked)"),
            ("UNMAPPABLE", self.unmappable, "(blocked)"),
        ]
        return [f"{label:17}{len(items):5d}  {note}".rstrip()
                for label, items, note in rows if items or label == "added"]


@dataclass
class Drift:
    """Result of one scan of the Data Panel.

    `present`, `names`, `files` and `untracked` are None if no scan ran. None
    means unknown, not empty.
    """
    missing: Set[str] = field(default_factory=set)
    occupied: Dict[str, str] = field(default_factory=dict)
    present: Optional[Set[str]] = None
    names: Optional[Dict[Tuple[str, ...], Set[str]]] = None
    files: Optional[int] = None                 # files in the folder
    duplicates: Dict[str, int] = field(default_factory=dict)  # "path/name": count
    untracked: Optional[int] = None             # files not in the manifest


def check_lines(check: Optional[Drift]) -> List[str]:
    """The folder check shown in Preview. Empty if the folder wasn't scanned."""
    if check is None or check.files is None:
        return []
    lines = [f"Folder check: {check.files} file(s), "
             f"{len(check.duplicates)} duplicate name(s), "
             f"{check.untracked} not tracked by Detent"]
    for path, n in sorted(check.duplicates.items())[:5]:
        lines.append(f"    {path}  (x{n})")
    if len(check.duplicates) > 5:
        lines.append(f"    ... and {len(check.duplicates) - 5} more")
    return lines


def upload_count(plan: "PL.Plan", report: Report) -> int:
    """Number of files a sync will actually upload: new plus missing, minus
    any refused because the name is taken."""
    return max(0, len(plan.to_place) - len(report.occupied))


def folder_change_impact(manifest: Optional[Manifest],
                         old_folder: str, new_folder: str) -> int:
    """Number of recorded files that would be uploaded again if the target
    folder changes. The manifest doesn't store the folder, so the caller
    passes both."""
    def parts(value: str) -> Tuple[str, ...]:
        return tuple(p for p in (value or "").split("/") if p)

    if parts(old_folder) == parts(new_folder):
        return 0
    return len(manifest.files) if manifest else 0


def reconcile_inflight(manifest: Manifest, panel: DataPanel, report: Report,
                       selected: Optional[Dict[str, str]] = None) -> None:
    """Confirm unconfirmed uploads by checking their folders.

    One file with that name: it landed; record its Fusion ID. If it's still
    uploading (no real ID yet), leave it for next time. None: drop it so it's
    retried. More than one: needs a human. `selected` supplies the blob.

    Each folder is listed once, however many files in it are waiting.
    """
    by_folder: Dict[Tuple[str, ...], List[Tuple[str, object, str]]] = {}
    for repo_path, entry in list(manifest.in_state(INFLIGHT).items()):
        try:
            pp = P.map_path(repo_path)
        except P.PathError:
            manifest.drop(repo_path)
            continue
        by_folder.setdefault(pp.folders, []).append(
            (repo_path, entry, entry.placed_name or pp.name))

    for folders, waiting in by_folder.items():
        try:
            folder = panel.peek_folder(folders)
            listing = panel.list_folder(folder) if folder is not None else []
        except Exception as exc:                      # noqa: BLE001
            # Still unconfirmed, so they won't be uploaded again.
            for repo_path, _entry, _name in waiting:
                report.failures.append(
                    (repo_path, f"couldn't check it in Fusion ({exc}) - "
                                "checked again on the next sync"))
            continue

        named: Dict[str, List] = {}
        for f in listing:
            named.setdefault(f.name, []).append(f)

        for repo_path, entry, name in waiting:
            hits = named.get(name, [])
            if len(hits) == 1 and not is_fusion_id(hits[0].lineage):
                report.reconciled.append((repo_path, "still uploading"))
            elif len(hits) == 1:
                blob = entry.blob or (selected or {}).get(repo_path)
                manifest.record(repo_path, blob, hits[0].lineage,
                                hits[0].version, state=PLACED,
                                placed_name=entry.placed_name)
                report.reconciled.append((repo_path, "landed"))
            elif not hits:
                manifest.drop(repo_path)
                report.reconciled.append((repo_path, "not present, will retry"))
            else:
                report.failures.append(
                    (repo_path, f"{len(hits)} files already named {name!r} - "
                                "resolve by hand before syncing"))


def detect_drift(manifest: Manifest, panel: DataPanel,
                 on_progress: Optional[Progress] = None) -> Drift:
    """Read every folder and find placed files that are no longer there.

    Slow on a big library: one request to Fusion per folder. Only the full
    check does this; an ordinary sync looks only at the folders it will put
    files into.

    missing   lineage gone, name free: safe to upload again
    occupied  lineage gone, another file has the name: report, don't upload
    present   every lineage found, so settle can tell our uploads apart
    names     every name per folder, so new files don't land on a taken name
    files, duplicates, untracked   the folder check shown in Preview

    If the scan fails, return an empty Drift. Treating a failed scan as
    "everything is gone" would re-upload the whole library.

    Maps paths without `subpath`, like apply_plan. adopt_existing uses it, so
    the two disagree if a subpath is set. Config rejects subpath for now.
    """
    def seen(count: int) -> None:
        if on_progress and on_progress(0, 0, f"Checking folder {count}") is False:
            raise gh.Cancelled("cancelled while checking folders")

    try:
        scan = panel.scan(seen)
    except gh.Cancelled:
        raise
    except Exception:                                  # noqa: BLE001
        return Drift()

    present = {f.lineage for files in scan.values() for f in files}
    names = {folders: {f.name for f in files} for folders, files in scan.items()}

    duplicates: Dict[str, int] = {}
    for folders, files in scan.items():
        for name, n in Counter(f.name for f in files).items():
            if n > 1:
                duplicates["/".join(folders + (name,))] = n
    tracked = {e.lineage for e in manifest.files.values() if e.lineage}
    untracked = sum(1 for files in scan.values() for f in files
                    if f.lineage not in tracked)

    missing: Set[str] = set()
    occupied: Dict[str, str] = {}
    for path, entry in manifest.files.items():
        if entry.state not in (PLACED, ADOPTED) or not entry.lineage:
            continue
        if entry.lineage in present:
            continue
        try:
            pp = P.map_path(path)
        except P.PathError:
            continue
        want = entry.placed_name or pp.name
        if want in names.get(pp.folders, frozenset()):
            occupied[path] = want
        else:
            missing.add(path)
    return Drift(missing=missing, occupied=occupied, present=present, names=names,
                 files=sum(len(v) for v in scan.values()),
                 duplicates=duplicates, untracked=untracked)


def settle(manifest: Manifest, panel: DataPanel, selected: Dict[str, str],
           report: Report, manifest_path: str, handles: Dict[str, object],
           on_progress: Optional[Progress], max_wait: float,
           mapped_by_path: Optional[Dict[str, object]] = None,
           pre_existing: Optional[Set[str]] = None) -> None:
    """Record the lineage of each started upload.

    An uploaded file shows up in its folder immediately, while Fusion's upload
    status takes 18-50 s. So check the folder first and ask the upload handle
    only for what the folder check misses.

    `pre_existing` is every lineage that was there before this run. Those are
    never claimed as ours.

    Never drops a manifest entry: the file may already be in the cloud, and
    the entry is what stops it being uploaded twice. Unresolved entries stay
    inflight for reconcile_inflight to check next run.

    Waits at most `max_wait` seconds. Whatever Fusion hasn't confirmed by
    then goes in report.pending. Cancel during the wait just stops waiting.

    With max_wait 0 it doesn't wait or read any folder. It asks each upload
    handle once, which is cheap, and leaves the rest for the next sync.
    """
    pre_existing = pre_existing or set()
    pending = dict(handles)
    deadline = time.time() + max_wait
    total = len(pending)
    first_pass = True

    def carry_on() -> bool:
        """Update the dialog. False once the user stops the wait."""
        done = total - len(pending)
        return not (on_progress and on_progress(
            done, total,
            f"Confirming {done}/{total} - Cancel to stop waiting") is False)

    def ask_handles() -> None:
        for repo_path, handle in list(pending.items()):
            try:
                placed = panel.poll_upload(handle)
            except UploadFailed as exc:
                # Stays inflight: it may still have landed.
                report.failures.append((repo_path, str(exc)))
                pending.pop(repo_path, None)
                continue
            if placed is not None and is_fusion_id(placed.lineage):
                manifest.record(repo_path, selected.get(repo_path, ""),
                                placed.lineage, placed.version, state=PLACED)
                report.added.append(repo_path)
                pending.pop(repo_path, None)

    if max_wait <= 0:
        if on_progress:
            on_progress(total, total, "Finishing")
        ask_handles()
        manifest.save(manifest_path)
        report.pending.extend(sorted(pending))
        return

    stopped = False
    while pending and time.time() < deadline and not stopped:
        # Check each folder once per pass.
        by_folder: Dict[Tuple[str, ...], List[Tuple[str, str]]] = {}
        for repo_path in pending:
            pp = (mapped_by_path or {}).get(repo_path)
            if pp is None:
                try:
                    pp = P.map_path(repo_path)
                except P.PathError:
                    manifest.drop(repo_path)
                    continue
            by_folder.setdefault(pp.folders, []).append((repo_path, pp.name))

        for folders, wanted in by_folder.items():
            # A big library has over a hundred folders, so check the time
            # and the Cancel button between them, not just between passes.
            if time.time() >= deadline or not carry_on():
                stopped = True
                break
            try:
                folder = panel.ensure_folder(folders)
                present: Dict[str, List] = {}
                for f in panel.list_folder(folder):
                    present.setdefault(f.name, []).append(f)
            except Exception as exc:                  # noqa: BLE001
                if first_pass:
                    report.failures.append(
                        (folders and "/".join(folders) or "<root>",
                         f"could not list folder: {type(exc).__name__}: {exc}"))
                continue

            for repo_path, name in wanted:
                hits = [h for h in present.get(name, [])
                        if h.lineage not in pre_existing]
                if any(not is_fusion_id(h.lineage) for h in hits):
                    continue                  # still uploading; check again next pass
                if len(hits) == 1:
                    manifest.record(repo_path, selected.get(repo_path, ""),
                                    hits[0].lineage, hits[0].version, state=PLACED)
                    report.added.append(repo_path)
                    pending.pop(repo_path, None)
                elif len(hits) > 1:
                    # Same name twice: the upload handle knows which is ours.
                    try:
                        placed = panel.poll_upload(pending[repo_path])
                    except UploadFailed as exc:
                        # Stays inflight: it may still have landed.
                        report.failures.append((repo_path, str(exc)))
                        pending.pop(repo_path, None)
                        continue
                    if placed is not None and is_fusion_id(placed.lineage):
                        manifest.record(repo_path, selected.get(repo_path, ""),
                                        placed.lineage, placed.version, state=PLACED)
                        report.added.append(repo_path)
                        pending.pop(repo_path, None)
                    elif time.time() >= deadline - 1:
                        report.failures.append(
                            (repo_path,
                             f"{len(hits)} files named {name!r}, upload did not "
                             "identify itself - left for the next run to reconcile"))
                        pending.pop(repo_path, None)

        if stopped:
            break

        # Ask the upload handle about anything not found in the folder.
        ask_handles()

        manifest.save(manifest_path)
        if not carry_on():
            break                             # the uploads carry on in Fusion
        first_pass = False
        if not pending:
            break
        panel.wait(1.0)

    manifest.save(manifest_path)
    report.pending.extend(sorted(pending))


def apply_plan(plan: PL.Plan, selected: Dict[str, str], src: gh.Source,
               manifest: Manifest, panel: DataPanel, manifest_path: str,
               transport: gh.Transport, commit: Optional[str] = None,
               workdir: Optional[str] = None,
               on_progress: Optional[Progress] = None,
               dry_run: bool = False,
               fallback_at: int = gh.ARCHIVE_FALLBACK_MIN,
               settle_wait: Optional[float] = None,
               known_lineages: Optional[Set[str]] = None,
               known_names: Optional[Dict[Tuple[str, ...], Set[str]]] = None,
               clock: Optional[Clock] = None) -> Report:
    """Upload new and missing files. Writes nothing when dry_run.

    known_lineages and known_names come from the full check. If None, only
    the folders that will receive files are listed here, and nothing is
    created to do it.
    """
    clock = clock or Clock()
    report = Report(
        skipped_changed=list(plan.change),
        skipped_orphan=list(plan.orphan),
        unverified=list(plan.unverified),
        conflicts=[(p, "gone from the Data Panel, but its name is taken - "
                       "resolve by hand") for p in plan.conflict],
    )
    # Missing files: the manifest says placed, the Data Panel disagrees.
    replace = set(plan.missing)

    # Map every path first. A collision or unmappable path blocks the run.
    mapped, errors, collisions = P.map_all(plan.to_place)
    report.unmappable = errors
    report.collisions = collisions
    # Reported in preview too: an unnoticed rename becomes a duplicate later.
    report.renamed = [(pp.repo_path, pp.name) for pp in mapped if pp.altered]
    if errors or collisions:
        return report

    if not mapped:
        return report

    # What's already in the folders that will receive files. A folder that
    # doesn't exist yet is empty, and isn't created here: Preview runs this.
    pre_existing: Set[str] = set(known_lineages or ())
    taken: Dict[Tuple[str, ...], Set[str]] = dict(known_names or {})
    if known_lineages is None or known_names is None:
        targets = sorted({pp.folders for pp in mapped})
        for i, folders in enumerate(targets):
            if on_progress and on_progress(
                    i, len(targets),
                    f"Looking in folder {i + 1}/{len(targets)}") is False:
                raise gh.Cancelled("cancelled while looking in folders")
            try:
                folder = panel.peek_folder(folders)
                listing = panel.list_folder(folder) if folder is not None else []
            except Exception:                         # noqa: BLE001
                # Can't list it: settle falls back to the upload handle.
                continue
            if known_lineages is None:
                pre_existing.update(f.lineage for f in listing)
            if known_names is None:
                taken[folders] = {f.name for f in listing}
    clock.lap("look")

    # Don't upload a new file onto a name that's already in the folder. The
    # manifest only knows files Detent placed, so this catches files added by
    # hand. Missing files are exempt: detect_drift already checked their names.
    # A folder that couldn't be listed isn't checked.
    blocked = {pp.repo_path for pp in mapped
               if pp.repo_path not in replace
               and pp.name in taken.get(pp.folders, ())}
    report.occupied = [(pp.repo_path, pp.name) for pp in mapped
                       if pp.repo_path in blocked]
    report.target_folders = len({pp.folders for pp in mapped
                                 if pp.repo_path not in blocked})

    if dry_run:
        return report

    # Open every target folder before downloading. If Fusion won't answer,
    # stop here instead of after the download.
    needed = sorted({pp.folders for pp in mapped if pp.repo_path not in blocked})
    for i, folders in enumerate(needed):
        if on_progress:
            if on_progress(i, len(needed),
                           f"Preparing folders {i + 1}/{len(needed)}") is False:
                raise gh.Cancelled("cancelled while preparing folders")
        try:
            panel.ensure_folder(folders)
        except Exception as exc:                      # noqa: BLE001
            report.failures.append(
                ("/".join(folders) or "(library folder)",
                 f"{exc}. Nothing was downloaded or uploaded. Wait a minute "
                 "and sync again. If it keeps happening, restart Fusion."))
            report.stopped_early = True
            return report
    clock.lap("folders")

    own_workdir = workdir is None
    workdir = workdir or tempfile.mkdtemp(prefix="detent-")
    try:
        wanted = [pp.repo_path for pp in mapped if pp.repo_path not in blocked]

        # fetch_files sets its own progress labels.
        fetched, fetch_failures = gh.fetch_files(
            src, wanted, workdir, transport, commit,
            on_progress=on_progress, fallback_at=fallback_at,
        )
        report.failures.extend(fetch_failures)
        clock.lap("download")

        total = len(wanted)
        handles: Dict[str, object] = {}
        sent = 0                              # files actually sent, for progress
        for pp in mapped:
            local = fetched.get(pp.repo_path)
            if not local:
                continue                      # already recorded as a fetch failure

            # Never upload a path the manifest has, unless it's missing.
            known = manifest.get(pp.repo_path)
            if (known is not None and known.state != INFLIGHT
                    and pp.repo_path not in replace):
                continue

            if pp.repo_path in blocked:
                continue

            try:
                folder = panel.ensure_folder(pp.folders)
            except Exception as exc:                  # noqa: BLE001
                # Not sent, not recorded: the next sync tries it again.
                report.failures.append((pp.repo_path, str(exc)))
                continue
            manifest.mark_inflight(pp.repo_path, placed_name=pp.name,
                                   blob=selected.get(pp.repo_path))
            manifest.save(manifest_path)

            sent += 1
            if on_progress:
                if on_progress(sent - 1, total,
                               f"Sending {sent}/{total}  {pp.name}") is False:
                    raise gh.Cancelled("cancelled during upload")

            try:
                handles[pp.repo_path] = panel.begin_upload(folder, local, pp.name)
            except UploadFailed as exc:
                manifest.drop(pp.repo_path)
                manifest.save(manifest_path)
                report.failures.append((pp.repo_path, str(exc)))
                continue

        clock.lap("send")
        if handles:
            wait = settle_wait if settle_wait is not None else CONFIRM_WAIT
            settle(manifest, panel, selected, report, manifest_path,
                   handles, on_progress, wait,
                   mapped_by_path={pp.repo_path: pp for pp in mapped},
                   pre_existing=pre_existing)
        clock.lap("confirm")

        report.replaced = [p for p in report.added + report.pending
                           if p in replace]

        manifest.save(manifest_path)
    finally:
        if own_workdir:
            shutil.rmtree(workdir, ignore_errors=True)

    return report


def sync(src: gh.Source, manifest_path: str, panel: DataPanel,
         transport: Optional[gh.Transport] = None,
         include: Sequence[str] = ("**/*.f3d",),
         exclude: Sequence[str] = (),
         dry_run: bool = True,
         on_progress: Optional[Progress] = None,
         fallback_at: int = gh.ARCHIVE_FALLBACK_MIN,
         settle_wait: Optional[float] = None,
         verify_placed: bool = True,
         commit: Optional[str] = None,
         recheck: Optional[bool] = None) -> Tuple[PL.Plan, Report]:
    """Run one full sync. Defaults to dry_run.

    verify_placed: the full check. Reads every folder to find files deleted
    by hand, duplicates and files Detent doesn't know. Slow on a big library.
    Without it, only the folders that will receive files are looked at.
    commit: use this commit instead of resolving `ref` again, so the upload
    matches the preview. The resolved commit is returned as Plan.commit.
    recheck: confirm earlier uploads Fusion hadn't finished. Updates Detent's
    record but nothing in Fusion. Defaults to on for a real run and off for a
    dry run; Verify Integrity turns it on for its dry run.
    """
    transport = transport or gh.UrllibTransport()
    clock = Clock()

    commit = commit or gh.resolve_commit(src, transport)
    tree = gh.fetch_tree(src, transport, commit)
    clock.lap("github")

    manifest = Manifest.load(manifest_path) or Manifest(
        source_id=src.repo.replace("/", "_"), repo=src.repo, ref=src.ref)

    selected = PL.select(tree, include, exclude)

    # v0.4.1 and earlier could record a file's temporary upload path instead
    # of its Fusion ID. Treat those as unconfirmed; the next sync confirms them.
    for entry in manifest.files.values():
        if entry.state in (PLACED, ADOPTED) and entry.lineage \
                and not is_fusion_id(entry.lineage):
            entry.state, entry.lineage = INFLIGHT, None

    pre = Report()
    if (not dry_run) if recheck is None else recheck:
        reconcile_inflight(manifest, panel, pre, selected)
        manifest.save(manifest_path)
    clock.lap("recheck")

    if not verify_placed and manifest.files:
        # One cheap request: if the whole library folder is gone, the record
        # can't be trusted, so do the full check (which is quick when there's
        # nothing to read).
        try:
            if panel.peek_folder(()) is None:
                verify_placed = True
        except Exception:                             # noqa: BLE001
            pass

    drift = (detect_drift(manifest, panel, on_progress) if verify_placed
             else Drift())
    clock.lap("check")
    plan = PL.diff(selected, manifest, missing=drift.missing)
    plan.conflict = sorted(drift.occupied)
    plan.commit = commit
    report = apply_plan(plan, selected, src, manifest, panel, manifest_path,
                        transport, commit, on_progress=on_progress,
                        dry_run=dry_run, fallback_at=fallback_at,
                        settle_wait=settle_wait, known_lineages=drift.present,
                        known_names=drift.names, clock=clock)
    refused = {p for p, _n in report.occupied}
    report.download_bytes = tree.bytes_for(
        [p for p in plan.to_place if p not in refused])
    report.check = drift if drift.files is not None else None

    # Record that the sync ran, even if nothing was uploaded. Skipped for
    # previews, cancelled runs (they raise before here) and blocked runs.
    blocked = bool(report.collisions or report.unmappable
                   or report.stopped_early)
    if not dry_run and not blocked:
        # Parts changed upstream are listed in this run's done box, then
        # dropped from the list: Detent never replaces them, and repeating
        # the list every run helps nobody. The user's file stays tracked.
        for path in plan.change:
            entry = manifest.files.get(path)
            if entry is not None and path in selected:
                entry.blob = selected[path]
        manifest.stamp(src.ref, commit)
        manifest.save(manifest_path)

    report.reconciled = pre.reconciled + report.reconciled
    report.failures = pre.failures + report.failures
    clock.lap("record")
    report.timings = dict(clock.parts)
    return plan, report


# How many recent releases Adopt compares against when it works out which
# one the user has. One GitHub request each.
MAX_RELEASES = 20


def adopt_existing(src: gh.Source, manifest_path: str, panel: DataPanel,
                   transport: gh.Transport, at_ref: Optional[str],
                   include: Sequence[str] = ("**/*.f3d",),
                   dry_run: bool = True,
                   on_progress: Optional[Progress] = None
                   ) -> Tuple[Manifest, Dict[str, object]]:
    """Record a library the user already has. Uploads nothing.

    at_ref is the release the user says they have, and is trusted. Without
    it, Detent works it out: it compares the part names in the folders with
    each recent release and picks the best match.

    Names can't show which version of a part someone has. So when Detent
    works the release out, each part is recorded as its oldest version up to
    that release. A part edited in a release then shows as changed upstream
    instead of looking current. Typing the release clears that.

    Every library part found is recorded, including parts from releases
    newer than the one picked, so Sync now never tries to add them again.

    If no release can be read or nothing matches, every part is recorded
    without a blob (unverified), matched against the current ref.

    Reads each folder once.
    """
    def tree_at(ref: str) -> Dict[str, str]:
        return PL.select(gh.fetch_tree(gh.Source(src.repo, ref, src.subpath),
                                       transport), include)

    try:
        tags = gh.list_releases(src, transport, MAX_RELEASES)
    except gh.Cancelled:
        raise
    except gh.GitHubError:
        tags = []
    releases = [(t, tree_at(t)) for t in tags]          # newest first
    trees: Dict[str, Dict[str, str]] = dict(releases)
    if at_ref is not None and at_ref not in trees:
        trees[at_ref] = tree_at(at_ref)
    current = tree_at(src.ref)

    # Oldest first: what a part looked like when it first appeared.
    history = [trees[t] for t, _tree in reversed(releases)]
    if at_ref is not None and at_ref not in dict(releases):
        history.append(trees[at_ref])
    history.append(current)

    # repo path -> (folders, name) in Fusion, for every part of every tree.
    where: Dict[str, Tuple[Tuple[str, ...], str]] = {}
    for tree in list(trees.values()) + [current]:
        for repo_path in tree:
            if repo_path in where:
                continue
            try:
                pp = P.map_path(repo_path, src.subpath)
            except P.PathError:
                continue
            where[repo_path] = (tuple(pp.folders), pp.name)

    # Read each folder once. Files still uploading have no real ID yet, so
    # they're skipped.
    present: Dict[Tuple[Tuple[str, ...], str], str] = {}
    in_panel = 0
    folders_to_read = sorted({f for f, _n in where.values()})
    for i, folders in enumerate(folders_to_read):
        if on_progress and on_progress(
                i, len(folders_to_read),
                f"Reading folder {i + 1}/{len(folders_to_read)}") is False:
            raise gh.Cancelled("cancelled while reading folders")
        folder = panel.peek_folder(folders)
        if folder is None:
            continue
        listing = panel.list_folder(folder)
        in_panel += len(listing)
        for f in listing:
            if is_fusion_id(f.lineage):
                present.setdefault((folders, f.name), f.lineage)

    def here(p: str) -> bool:
        return p in where and where[p] in present

    def score(tree: Dict[str, str]) -> Tuple[int, int]:
        mine = {where[p] for p in tree if p in where}
        hit = sum(1 for spot in mine if spot in present)
        extra = sum(1 for spot in present if spot not in mine)
        return (hit - extra, -(len(tree) - hit))

    release: Optional[str] = at_ref
    detected = False
    tied: List[str] = []
    if at_ref is None and releases:
        scores = [score(tree) for _t, tree in releases]
        best = max(scores)
        first = scores.index(best)
        if any(here(p) for p in releases[first][1]):
            release, detected = releases[first][0], True
            tied = [t for (t, _tree), sc in zip(releases, scores)
                    if sc == best and t != release]

    if release is None:
        # Nothing to compare with, or nothing matched: record what's there
        # against the current ref, without claiming which version it is.
        chosen = current
        local = {p: present[where[p]] for p in chosen if here(p)}
        blobs: Dict[str, Optional[str]] = {p: None for p in local}
        stats: Dict[str, object] = {
            "matched": len(local), "in_release": len(chosen),
            "in_panel": in_panel, "missing": len(chosen) - len(local),
            "release": None, "detected": False, "tied": [],
            "unverified": len(local), "assumed_older": 0, "newer": 0,
        }
        manifest = adopt(src.repo.replace("/", "_"), src.repo, src.ref,
                         blobs, local, release_known=True)
        if not dry_run:
            manifest.save(manifest_path)
        return manifest, stats

    chosen = trees[release]
    if detected:
        # Releases up to and including the one picked, oldest first.
        upto = [tree for t, tree in reversed(releases)]
        upto = upto[:len(releases) - [t for t, _x in releases].index(release)]
    else:
        upto = [chosen]

    def oldest(p: str, among) -> Optional[str]:
        for tree in among:
            if p in tree:
                return tree[p]
        return None

    local: Dict[str, str] = {}
    blobs = {}
    taken: Set[Tuple[Tuple[str, ...], str]] = set()
    # The picked release first, then any other part of the library.
    others = sorted({p for tree in history for p in tree} - set(chosen))
    for p in list(chosen) + others:
        if not here(p) or where[p] in taken:
            continue
        taken.add(where[p])
        local[p] = present[where[p]]
        blobs[p] = oldest(p, upto) if p in chosen else oldest(p, history)

    stats = {
        "matched": len(local),
        "in_release": len(chosen),
        "in_panel": in_panel,
        "missing": sum(1 for p in chosen if p not in local),
        "release": release,
        "detected": detected,
        "tied": tied,
        "unverified": sum(1 for p in local if blobs.get(p) is None),
        "assumed_older": sum(1 for p in local
                             if p in chosen and blobs[p] != chosen[p]),
        "newer": sum(1 for p in local if p not in chosen),
    }

    manifest = adopt(src.repo.replace("/", "_"), src.repo, src.ref,
                     blobs, local, release_known=True)
    if not dry_run:
        manifest.save(manifest_path)
    return manifest, stats
