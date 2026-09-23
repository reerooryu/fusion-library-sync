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
from .datapanel import DataPanel, UploadFailed
from .manifest import Manifest, ADOPTED, INFLIGHT, PLACED, adopt


Progress = Callable[[int, int, str], None]


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

    @property
    def ok(self) -> bool:
        return not (self.failures or self.collisions
                    or self.unmappable or self.conflicts or self.occupied)

    def lines(self) -> List[str]:
        rows = [
            ("added", self.added, ""),
            ("re-placed", self.replaced, "(were gone from the Data Panel)"),
            ("changed upstream", self.skipped_changed, "(Phase 2)"),
            ("gone upstream", self.skipped_orphan, "(cannot delete)"),
            ("unverified", self.unverified, ""),
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
    """Settle uploads interrupted by a crash by checking the folder.

    One file with that name: it landed, record it. None: drop it so it's
    retried. More than one: needs a human. `selected` supplies the blob.
    """
    for repo_path, entry in list(manifest.in_state(INFLIGHT).items()):
        try:
            pp = P.map_path(repo_path)
        except P.PathError:
            manifest.drop(repo_path)
            continue
        folder = panel.ensure_folder(pp.folders)
        hits = panel.find_by_name(folder, entry.placed_name or pp.name)

        if len(hits) == 1:
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
                (repo_path, f"{len(hits)} files already named {pp.name!r} - "
                            "resolve by hand before syncing"))


def detect_drift(manifest: Manifest, panel: DataPanel) -> Drift:
    """Find placed files that are no longer in the Data Panel.

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
    try:
        scan = panel.scan()
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
    """
    pre_existing = pre_existing or set()
    pending = dict(handles)
    deadline = time.time() + max_wait
    total = len(pending)
    first_pass = True

    while pending and time.time() < deadline:
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
                    if placed is not None:
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

        # Ask the upload handle about anything not found in the folder.
        for repo_path, handle in list(pending.items()):
            try:
                placed = panel.poll_upload(handle)
            except UploadFailed as exc:
                # Stays inflight: it may still have landed.
                report.failures.append((repo_path, str(exc)))
                pending.pop(repo_path, None)
                continue
            if placed is not None:
                manifest.record(repo_path, selected.get(repo_path, ""),
                                placed.lineage, placed.version, state=PLACED)
                report.added.append(repo_path)
                pending.pop(repo_path, None)

        manifest.save(manifest_path)
        done = total - len(pending)
        if on_progress:
            if on_progress(done, total, f"Finishing {done}/{total}") is False:
                raise gh.Cancelled("cancelled while finishing")
        first_pass = False
        if not pending:
            break
        time.sleep(1.0)

    for repo_path in pending:
        report.failures.append(
            (repo_path, f"upload unresolved after {max_wait:.0f}s"))


def apply_plan(plan: PL.Plan, selected: Dict[str, str], src: gh.Source,
               manifest: Manifest, panel: DataPanel, manifest_path: str,
               transport: gh.Transport, commit: Optional[str] = None,
               workdir: Optional[str] = None,
               on_progress: Optional[Progress] = None,
               dry_run: bool = False,
               fallback_at: int = gh.ARCHIVE_FALLBACK_MIN,
               settle_wait: Optional[float] = None,
               known_lineages: Optional[Set[str]] = None,
               known_names: Optional[Dict[Tuple[str, ...], Set[str]]] = None
               ) -> Report:
    """Upload new and missing files. Writes nothing when dry_run.

    known_lineages and known_names come from the drift scan. If None, each
    target folder is listed here instead.
    """
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

    # What's already in the target folders. Preview only uses the drift scan;
    # it doesn't list folders itself.
    pre_existing: Set[str] = set(known_lineages or ())
    taken: Dict[Tuple[str, ...], Set[str]] = dict(known_names or {})
    if (known_lineages is None or known_names is None) and not dry_run:
        for folders in {pp.folders for pp in mapped}:
            try:
                listing = panel.list_folder(panel.ensure_folder(folders))
            except Exception:                         # noqa: BLE001
                # Can't list it: settle falls back to the upload handle.
                continue
            if known_lineages is None:
                pre_existing.update(f.lineage for f in listing)
            if known_names is None:
                taken[folders] = {f.name for f in listing}

    # Don't upload a new file onto a name that's already in the folder. The
    # manifest only knows files Detent placed, so this catches files added by
    # hand. Missing files are exempt: detect_drift already checked their names.
    # A folder that couldn't be listed isn't checked.
    blocked = {pp.repo_path for pp in mapped
               if pp.repo_path not in replace
               and pp.name in taken.get(pp.folders, ())}
    report.occupied = [(pp.repo_path, pp.name) for pp in mapped
                       if pp.repo_path in blocked]

    if dry_run:
        return report

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

            folder = panel.ensure_folder(pp.folders)
            manifest.mark_inflight(pp.repo_path, placed_name=pp.name)
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

        if handles:
            # One second per upload, at least 300. Fusion processes uploads in
            # a queue, so big batches need longer.
            wait = settle_wait if settle_wait is not None else max(300.0, float(len(handles)))
            settle(manifest, panel, selected, report, manifest_path,
                   handles, on_progress, wait,
                   mapped_by_path={pp.repo_path: pp for pp in mapped},
                   pre_existing=pre_existing)

        report.replaced = [p for p in report.added if p in replace]

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
         commit: Optional[str] = None) -> Tuple[PL.Plan, Report]:
    """Run one full sync. Defaults to dry_run.

    verify_placed: check the Data Panel for files deleted by hand.
    commit: use this commit instead of resolving `ref` again, so the upload
    matches the preview. The resolved commit is returned as Plan.commit.
    """
    transport = transport or gh.UrllibTransport()

    commit = commit or gh.resolve_commit(src, transport)
    tree = gh.fetch_tree(src, transport, commit)

    manifest = Manifest.load(manifest_path) or Manifest(
        source_id=src.repo.replace("/", "_"), repo=src.repo, ref=src.ref)

    selected = PL.select(tree, include, exclude)

    pre = Report()
    if not dry_run:
        reconcile_inflight(manifest, panel, pre, selected)
        manifest.save(manifest_path)

    drift = detect_drift(manifest, panel) if verify_placed else Drift()
    plan = PL.diff(selected, manifest, missing=drift.missing)
    plan.conflict = sorted(drift.occupied)
    plan.commit = commit
    report = apply_plan(plan, selected, src, manifest, panel, manifest_path,
                        transport, commit, on_progress=on_progress,
                        dry_run=dry_run, fallback_at=fallback_at,
                        settle_wait=settle_wait, known_lineages=drift.present,
                        known_names=drift.names)
    refused = {p for p, _n in report.occupied}
    report.download_bytes = tree.bytes_for(
        [p for p in plan.to_place if p not in refused])
    report.check = drift if drift.files is not None else None

    # Record that the sync ran, even if nothing was uploaded. Skipped for
    # previews, cancelled runs (they raise before here) and blocked runs.
    blocked = bool(report.collisions or report.unmappable)
    if not dry_run and not blocked:
        manifest.stamp(src.ref, commit)
        manifest.save(manifest_path)

    report.reconciled = pre.reconciled + report.reconciled
    report.failures = pre.failures + report.failures
    return plan, report


def adopt_existing(src: gh.Source, manifest_path: str, panel: DataPanel,
                   transport: gh.Transport, at_ref: Optional[str],
                   include: Sequence[str] = ("**/*.f3d",),
                   dry_run: bool = True) -> Tuple[Manifest, Dict[str, int]]:
    """Record a library the user already has. Uploads nothing.

    With at_ref=None, files are recorded without a blob (unverified).
    """
    release_known = at_ref is not None
    # Without a release, match against the current ref.
    match_ref = at_ref or src.ref
    tree = gh.fetch_tree(gh.Source(src.repo, match_ref, src.subpath), transport)
    tree = PL.select(tree, include)

    panel_contents = panel.scan()
    local: Dict[str, str] = {}
    by_name: Dict[Tuple[Tuple[str, ...], str], str] = {}
    for folders, files in panel_contents.items():
        for f in files:
            by_name[(folders, f.name)] = f.lineage

    for repo_path in tree:
        try:
            pp = P.map_path(repo_path, src.subpath)
        except P.PathError:
            continue
        lineage = by_name.get((pp.folders, pp.name))
        if lineage:
            local[repo_path] = lineage

    stats = {
        "matched": len(local),
        "in_release": len(tree),
        "in_panel": sum(len(v) for v in panel_contents.values()),
    }
    stats["missing"] = stats["in_release"] - stats["matched"]

    manifest = adopt(src.repo.replace("/", "_"), src.repo, src.ref,
                     tree, local, release_known=release_known)
    if not dry_run:
        manifest.save(manifest_path)
    return manifest, stats
