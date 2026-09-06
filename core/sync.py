"""Orchestration: plan -> confirm -> apply.

Phase 1 writes exactly one kind of thing: files new to the target. Changed,
orphaned and unverified paths are reported and left alone.

The invariant, restated because everything here serves it:

    Never upload a path already recorded in the manifest.

Fusion will not stop us. The manifest is the only guard, so it is written
before an upload starts (inflight) and again once the upload is confirmed
(placed), and it is flushed to disk after every file.
"""

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple
import os
import shutil
import tempfile

from . import github as gh
from . import paths as P
from . import plan as PL
from .datapanel import DataPanel, PlacedFile, UploadFailed
from .manifest import Manifest, INFLIGHT, PLACED, ADOPTED, adopt


Progress = Callable[[int, int, str], None]


@dataclass
class Report:
    added: List[str] = field(default_factory=list)
    skipped_changed: List[str] = field(default_factory=list)
    skipped_orphan: List[str] = field(default_factory=list)
    unverified: List[str] = field(default_factory=list)
    failures: List[Tuple[str, str]] = field(default_factory=list)
    renamed: List[Tuple[str, str]] = field(default_factory=list)
    reconciled: List[Tuple[str, str]] = field(default_factory=list)
    collisions: Dict[str, List[str]] = field(default_factory=dict)
    unmappable: List[Tuple[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures and not self.collisions and not self.unmappable

    def lines(self) -> List[str]:
        out = [f"added            {len(self.added)}"]
        if self.skipped_changed:
            out.append(f"changed upstream {len(self.skipped_changed)}  (Phase 2)")
        if self.skipped_orphan:
            out.append(f"gone upstream    {len(self.skipped_orphan)}  (cannot delete)")
        if self.unverified:
            out.append(f"unverified       {len(self.unverified)}")
        if self.reconciled:
            out.append(f"reconciled       {len(self.reconciled)}")
        if self.renamed:
            out.append(f"renamed          {len(self.renamed)}  (see detail)")
        if self.failures:
            out.append(f"failed           {len(self.failures)}")
        if self.collisions:
            out.append(f"COLLISIONS       {len(self.collisions)}  (sync blocked)")
        if self.unmappable:
            out.append(f"UNMAPPABLE       {len(self.unmappable)}  (sync blocked)")
        return out


def reconcile_inflight(manifest: Manifest, panel: DataPanel,
                       report: Report) -> None:
    """Resolve uploads interrupted by a crash.

    The manifest cannot be trusted for these - the truth is in the cloud, so
    go and look. Exactly one match means it landed; none means it is safe to
    retry; more than one means a duplicate already exists and a human must
    decide.
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
            manifest.record(repo_path, entry.blob or "", hits[0].lineage,
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


def make_plan(tree: Dict[str, str], manifest: Optional[Manifest],
              include: Sequence[str], exclude: Sequence[str] = ()) -> Tuple[PL.Plan, Dict[str, str]]:
    selected = PL.select(tree, include, exclude)
    return PL.diff(selected, manifest), selected


def apply_plan(plan: PL.Plan, selected: Dict[str, str], src: gh.Source,
               manifest: Manifest, panel: DataPanel, manifest_path: str,
               transport: gh.Transport, commit: Optional[str] = None,
               workdir: Optional[str] = None,
               on_progress: Optional[Progress] = None,
               dry_run: bool = False) -> Report:
    """Execute the additive half of a plan. Writes nothing when dry_run."""
    report = Report(
        skipped_changed=list(plan.change),
        skipped_orphan=list(plan.orphan),
        unverified=list(plan.unverified),
    )

    # Map every path first. A collision or an unmappable path blocks the run:
    # two repo files landing on one Data Panel name is how a library silently
    # loses a part.
    mapped, errors, collisions = P.map_all(plan.add)
    report.unmappable = errors
    report.collisions = collisions
    if errors or collisions:
        return report

    if dry_run or not mapped:
        return report

    own_workdir = workdir is None
    workdir = workdir or tempfile.mkdtemp(prefix="lockstep-")
    try:
        wanted = [pp.repo_path for pp in mapped]
        fetched, fetch_failures = gh.fetch_files(
            src, wanted, workdir, transport, commit,
            on_progress=lambda i, n, p: on_progress and on_progress(i, n, f"download {p}"),
        )
        report.failures.extend(fetch_failures)

        total = len(mapped)
        for i, pp in enumerate(mapped, 1):
            local = fetched.get(pp.repo_path)
            if not local:
                continue                      # already recorded as a fetch failure

            # Belt and braces: never upload something the manifest knows.
            if pp.repo_path in manifest and manifest.get(pp.repo_path).state != INFLIGHT:
                continue

            folder = panel.ensure_folder(pp.folders)
            manifest.mark_inflight(pp.repo_path, placed_name=pp.name)
            manifest.save(manifest_path)

            try:
                placed = panel.upload(folder, local, pp.name)
            except UploadFailed as exc:
                manifest.drop(pp.repo_path)
                manifest.save(manifest_path)
                report.failures.append((pp.repo_path, str(exc)))
                continue

            if placed.name != pp.name:
                report.renamed.append((pp.repo_path, placed.name))

            manifest.record(pp.repo_path, selected[pp.repo_path], placed.lineage,
                            placed.version, state=PLACED,
                            placed_name=placed.name if placed.name != pp.name else None)
            manifest.save(manifest_path)      # after every file, not at the end
            report.added.append(pp.repo_path)

            if on_progress:
                on_progress(i, total, pp.name)

        manifest.synced_commit = commit
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
         on_progress: Optional[Progress] = None) -> Tuple[PL.Plan, Report]:
    """One full cycle. Defaults to dry_run - callers opt in to writing."""
    transport = transport or gh.UrllibTransport()

    commit = gh.resolve_commit(src, transport)
    tree, _rate = gh.fetch_tree(src, transport, commit)

    manifest = Manifest.load(manifest_path) or Manifest(
        source_id=src.repo.replace("/", "_"), repo=src.repo, ref=src.ref)

    pre = Report()
    if not dry_run:
        reconcile_inflight(manifest, panel, pre)
        manifest.save(manifest_path)

    plan, selected = make_plan(tree, manifest, include, exclude)
    report = apply_plan(plan, selected, src, manifest, panel, manifest_path,
                        transport, commit, on_progress=on_progress, dry_run=dry_run)
    report.reconciled = pre.reconciled + report.reconciled
    report.failures = pre.failures + report.failures
    return plan, report


def adopt_existing(src: gh.Source, manifest_path: str, panel: DataPanel,
                   transport: gh.Transport, at_ref: Optional[str],
                   include: Sequence[str] = ("**/*.f3d",),
                   dry_run: bool = True) -> Tuple[Manifest, Dict[str, int]]:
    """Claim a library the user already imported. Uploads nothing.

    at_ref=None means "I don't know which release" - lineages are recorded
    with no blob, so nothing is claimed about content and the files defer to
    Phase 2 rather than being guessed at.
    """
    release_known = at_ref is not None
    tree: Dict[str, str] = {}
    if release_known:
        tree, _ = gh.fetch_tree(gh.Source(src.repo, at_ref, src.subpath), transport)
        tree = PL.select(tree, include)

    # Data Panel contents keyed the same way the manifest is.
    local: Dict[str, str] = {}
    by_name: Dict[Tuple[Tuple[str, ...], str], str] = {}
    for folders, files in panel.scan().items():
        for f in files:
            by_name[(folders, f.name)] = f.lineage

    candidates = tree if release_known else {}
    for repo_path in (candidates or {}):
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
        "in_panel": sum(len(v) for v in panel.scan().values()),
    }
    stats["missing"] = stats["in_release"] - stats["matched"]

    manifest = adopt(src.repo.replace("/", "_"), src.repo, src.ref,
                     tree, local, release_known=release_known)
    if not dry_run:
        manifest.save(manifest_path)
    return manifest, stats
