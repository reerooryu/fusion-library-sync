"""Orchestration: plan -> confirm -> apply.

Phase 1 writes one kind of thing: files new to the target. Everything else is
reported.

    INVARIANT: never upload a path already recorded in the manifest.

Fusion will not stop us, so the manifest is the only guard: written before an
upload (inflight), again once confirmed (placed), flushed after every file.
"""

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

    @property
    def ok(self) -> bool:
        return not (self.failures or self.collisions
                    or self.unmappable or self.conflicts)

    def lines(self) -> List[str]:
        rows = [
            ("added", self.added, ""),
            ("re-placed", self.replaced, "(were gone from the Data Panel)"),
            ("changed upstream", self.skipped_changed, "(Phase 2)"),
            ("gone upstream", self.skipped_orphan, "(cannot delete)"),
            ("unverified", self.unverified, ""),
            ("reconciled", self.reconciled, ""),
            ("renamed", self.renamed, "(see detail)"),
            ("CONFLICTS", self.conflicts, "(needs a human)"),
            ("failed", self.failures, ""),
            ("COLLISIONS", self.collisions, "(blocked)"),
            ("UNMAPPABLE", self.unmappable, "(blocked)"),
        ]
        return [f"{label:17}{len(items):5d}  {note}".rstrip()
                for label, items, note in rows if items or label == "added"]


def reconcile_inflight(manifest: Manifest, panel: DataPanel, report: Report,
                       selected: Optional[Dict[str, str]] = None) -> None:
    """Resolve uploads interrupted by a crash by looking in the cloud, not the
    manifest. One match landed; none is safe to retry; more needs a human.

    `selected` supplies the blob for a file that landed. Without it the entry
    was recorded with blob="" - not None, so never 'unverified', and never
    equal to any real SHA - which reported the file as changed upstream on
    every single sync from then on.
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


def detect_drift(manifest: Manifest, panel: DataPanel
                 ) -> Tuple[Set[str], Dict[str, str], Set[str]]:
    """Find manifest entries the Data Panel no longer backs up.

    Returns (missing, occupied):
      missing  - the lineage is gone and nothing holds its name. Safe to
                 re-place.
      occupied - the lineage is gone but something else now sits under that
                 name. NOT safe to re-place, and a human has to look.
      present  - every lineage in the subtree right now. settle uses it to
                 tell our own uploads from files that were already there,
                 which saves a second walk of the same folders.

    Without this a deleted folder leaves Detent reporting 'up to date' over an
    empty shelf, which is the worst thing a sync tool can be confidently wrong
    about.

    Identity is the lineage, never the name: a name match is satisfied by any
    file that happens to share it. But the name still has to be checked before
    re-placing, because re-placing is the one path that uploads over a manifest
    entry - relax that bar on lineage alone and this function becomes a way to
    manufacture the very duplicates the tool exists to prevent.

    A scan that fails returns nothing rather than everything. Read as 'it is
    all gone', one transient API error would re-upload an entire library on top
    of itself. A false negative costs a stale row; a false positive costs
    duplicates nobody can undo.

    NOTE: maps without the source subpath, matching apply_plan and
    reconcile_inflight. adopt_existing maps WITH it, so the two disagree for a
    source that sets one. Unused today; tracked separately.
    """
    try:
        scan = panel.scan()
    except Exception:                                  # noqa: BLE001
        return set(), {}, set()

    present = {f.lineage for files in scan.values() for f in files}
    names = {folders: {f.name for f in files} for folders, files in scan.items()}

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
    return missing, occupied, present


def settle(manifest: Manifest, panel: DataPanel, selected: Dict[str, str],
           report: Report, manifest_path: str, handles: Dict[str, object],
           on_progress: Optional[Progress], max_wait: float,
           mapped_by_path: Optional[Dict[str, object]] = None,
           pre_existing: Optional[Set[str]] = None) -> None:
    """Resolve fired uploads into recorded lineages.

    Measured 6 Sep 2026: a fired file appears in folder.dataFiles immediately
    - first_appeared_s was 0.0 for all five - and the collection is live, so
    no refresh is needed (DataFolder has no refresh() anyway). Meanwhile
    uploadState took 18-50s to leave Processing.

    So: scan the folder first, which is instant. Fall back to polling the
    future only for anything the scan does not find.

    `pre_existing` is the set of lineages present BEFORE this run fired
    anything. A name hit that was already there is somebody else's file, and
    claiming its lineage would leave the manifest owning a file we never
    created - while ours sits beside it unrecorded.

    Nothing here drops a manifest entry. Every path in this function has
    already called begin_upload, so the file may well be in the cloud; the
    entry stays inflight and reconcile_inflight settles it next run by
    looking. Dropping it removes the only guard against re-uploading, which
    is how a folder ends up with three files of one name.
    """
    pre_existing = pre_existing or set()
    pending = dict(handles)
    deadline = time.time() + max_wait
    total = len(pending)
    first_pass = True

    while pending and time.time() < deadline:
        # --- fast path: one live listing per folder, no refresh
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
                # Only files that were not already there can be ours.
                hits = [h for h in present.get(name, [])
                        if h.lineage not in pre_existing]
                if len(hits) == 1:
                    manifest.record(repo_path, selected.get(repo_path, ""),
                                    hits[0].lineage, hits[0].version, state=PLACED)
                    report.added.append(repo_path)
                    pending.pop(repo_path, None)
                elif len(hits) > 1:
                    # Ambiguous by name - so ask the future, which knows which
                    # file WE created. Only give up if it cannot tell us.
                    try:
                        placed = panel.poll_upload(pending[repo_path])
                    except UploadFailed as exc:
                        # Left inflight on purpose: it may still have landed.
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

        # --- fallback: ask the future about whatever the scan missed
        for repo_path, handle in list(pending.items()):
            try:
                placed = panel.poll_upload(handle)
            except UploadFailed as exc:
                # Left inflight on purpose: it may still have landed.
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
               threshold: int = gh.TARBALL_THRESHOLD,
               settle_wait: Optional[float] = None,
               known_lineages: Optional[Set[str]] = None,
               want_bytes: int = 0, total_bytes: int = 0) -> Report:
    """Additive half of a plan. Writes nothing when dry_run.

    known_lineages: every lineage already in the target subtree, from the
    drift scan sync() has just done. Passed on to settle so it can tell our
    uploads from files that were there first. None means take a listing here.
    """
    report = Report(
        skipped_changed=list(plan.change),
        skipped_orphan=list(plan.orphan),
        unverified=list(plan.unverified),
        conflicts=[(p, "gone from the Data Panel, but its name is taken - "
                       "resolve by hand") for p in plan.conflict],
    )
    # Files the manifest claims but the Data Panel does not have. The stale
    # entry is the thing standing between them and a re-upload, so it goes.
    replace = set(plan.missing)

    # Map every path first. A collision or an unmappable path blocks the run:
    # two repo files landing on one Data Panel name is how a library silently
    # loses a part.
    mapped, errors, collisions = P.map_all(plan.to_place)
    report.unmappable = errors
    report.collisions = collisions
    # An altered name that nobody sees becomes a duplicate on the next sync,
    # because the manifest keys on the repo path and the panel keys on the
    # name. Surface it in preview as well as after writing.
    report.renamed = [(pp.repo_path, pp.name) for pp in mapped if pp.altered]
    if errors or collisions:
        return report

    if dry_run or not mapped:
        return report

    own_workdir = workdir is None
    workdir = workdir or tempfile.mkdtemp(prefix="detent-")
    try:
        wanted = [pp.repo_path for pp in mapped]

        # fetch_files owns its own labels: it is the only layer that knows
        # whether this is one archive or N files, and how far along it is.
        # Rewriting the label here is what discarded the byte percentage.
        fetched, fetch_failures = gh.fetch_files(
            src, wanted, workdir, transport, commit,
            on_progress=on_progress, threshold=threshold,
            want_bytes=want_bytes, total_bytes=total_bytes,
        )
        report.failures.extend(fetch_failures)

        # Everything already in the target folders, before we add to them.
        # settle uses this to tell our uploads from files that were there
        # first; without it a single name hit is claimed on sight.
        pre_existing: Set[str] = set(known_lineages or ())
        if known_lineages is None:
            for folders in {pp.folders for pp in mapped}:
                try:
                    for f in panel.list_folder(panel.ensure_folder(folders)):
                        pre_existing.add(f.lineage)
                except Exception:                     # noqa: BLE001
                    # Unreadable folder: we cannot prove ownership there, so
                    # settle falls back to the future - slower and correct.
                    pass

        total = len(mapped)
        handles: Dict[str, object] = {}
        for i, pp in enumerate(mapped, 1):
            local = fetched.get(pp.repo_path)
            if not local:
                continue                      # already recorded as a fetch failure

            # Belt and braces: never upload something the manifest knows -
            # unless we have just confirmed the file it points at is gone.
            known = manifest.get(pp.repo_path)
            if (known is not None and known.state != INFLIGHT
                    and pp.repo_path not in replace):
                continue

            folder = panel.ensure_folder(pp.folders)
            manifest.mark_inflight(pp.repo_path, placed_name=pp.name)
            manifest.save(manifest_path)

            if on_progress:
                if on_progress(i - 1, total,
                               f"Sending {i}/{total}  {pp.name}") is False:
                    raise gh.Cancelled("cancelled during upload")

            try:
                handles[pp.repo_path] = panel.begin_upload(folder, local, pp.name)
            except UploadFailed as exc:
                manifest.drop(pp.repo_path)
                manifest.save(manifest_path)
                report.failures.append((pp.repo_path, str(exc)))
                continue

        if handles:
            # One second per fired upload, floor 300. A flat 300 was fine for
            # 45 files and left the tail of a 1,198-file batch unresolved:
            # every upload is fired at once, so the last one to be processed
            # waits behind all the others in Fusion's own queue. The loop polls
            # and yields once a second, so a longer deadline costs nothing when
            # everything settles early.
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
         threshold: int = gh.TARBALL_THRESHOLD,
         settle_wait: Optional[float] = None,
         verify_placed: bool = True) -> Tuple[PL.Plan, Report]:
    """One full cycle. Defaults to dry_run.

    verify_placed walks the Data Panel to check the manifest is still telling
    the truth. It costs one listing per folder; turning it off makes a sync
    trust its own bookkeeping, which is fine right up until someone deletes a
    folder by hand.
    """
    transport = transport or gh.UrllibTransport()

    commit = gh.resolve_commit(src, transport)
    tree = gh.fetch_tree(src, transport, commit)

    manifest = Manifest.load(manifest_path) or Manifest(
        source_id=src.repo.replace("/", "_"), repo=src.repo, ref=src.ref)

    selected = PL.select(tree, include, exclude)

    pre = Report()
    if not dry_run:
        reconcile_inflight(manifest, panel, pre, selected)
        manifest.save(manifest_path)

    gone, occupied, present = (detect_drift(manifest, panel) if verify_placed
                               else (set(), {}, None))
    plan = PL.diff(selected, manifest, missing=gone)
    plan.conflict = sorted(occupied)
    report = apply_plan(plan, selected, src, manifest, panel, manifest_path,
                        transport, commit, on_progress=on_progress,
                        dry_run=dry_run, threshold=threshold,
                        settle_wait=settle_wait, known_lineages=present,
                        want_bytes=getattr(tree, "bytes_for", lambda _p: 0)(selected),
                        total_bytes=getattr(tree, "total_bytes", 0))

    # Stamp last, and here rather than inside apply_plan, which returns early
    # on every path that has nothing to upload. A run that finds nothing still
    # checked, and the manifest is the only place that fact can live.
    # Cancellation raises out of apply_plan, so an aborted run never stamps.
    # A run blocked before it wrote anything has not reconciled this ref with
    # anything, so it must not leave a header saying it did. Upload failures
    # are different: the tree WAS compared, and the per-file entries record
    # what actually landed.
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
    """Claim a library the user already imported. Uploads nothing.
    at_ref=None records lineages with no blob rather than guessing."""
    release_known = at_ref is not None
    # Paths to match on always come from a tree - at the named release when we
    # have one, otherwise at the current ref. Without this an unknown-release
    # adopt has nothing to match and silently records an empty manifest.
    match_ref = at_ref or src.ref
    tree = gh.fetch_tree(gh.Source(src.repo, match_ref, src.subpath), transport)
    tree = PL.select(tree, include)

    # One walk of the Data Panel - it is expensive against the real API.
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
