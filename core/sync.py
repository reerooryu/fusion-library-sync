"""Orchestration: plan -> confirm -> apply.

Phase 1 writes one kind of thing: files new to the target. Everything else is
reported.

    INVARIANT: never upload a path already recorded in the manifest.

Fusion will not stop us, so the manifest is the only guard: written before an
upload (inflight), again once confirmed (placed), flushed after every file.
"""

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple
import shutil
import tempfile

from . import github as gh
from . import paths as P
from . import plan as PL
from .datapanel import DataPanel, UploadFailed
from .manifest import Manifest, INFLIGHT, PLACED, adopt


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
        rows = [
            ("added", self.added, ""),
            ("changed upstream", self.skipped_changed, "(Phase 2)"),
            ("gone upstream", self.skipped_orphan, "(cannot delete)"),
            ("unverified", self.unverified, ""),
            ("reconciled", self.reconciled, ""),
            ("renamed", self.renamed, "(see detail)"),
            ("failed", self.failures, ""),
            ("COLLISIONS", self.collisions, "(blocked)"),
            ("UNMAPPABLE", self.unmappable, "(blocked)"),
        ]
        return [f"{label:17}{len(items):5d}  {note}".rstrip()
                for label, items, note in rows if items or label == "added"]


def reconcile_inflight(manifest: Manifest, panel: DataPanel,
                       report: Report) -> None:
    """Resolve uploads interrupted by a crash by looking in the cloud, not the
    manifest. One match landed; none is safe to retry; more needs a human."""
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


def apply_plan(plan: PL.Plan, selected: Dict[str, str], src: gh.Source,
               manifest: Manifest, panel: DataPanel, manifest_path: str,
               transport: gh.Transport, commit: Optional[str] = None,
               workdir: Optional[str] = None,
               on_progress: Optional[Progress] = None,
               dry_run: bool = False) -> Report:
    """Additive half of a plan. Writes nothing when dry_run."""
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
    workdir = workdir or tempfile.mkdtemp(prefix="detent-")
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
    """One full cycle. Defaults to dry_run."""
    transport = transport or gh.UrllibTransport()

    commit = gh.resolve_commit(src, transport)
    tree = gh.fetch_tree(src, transport, commit)

    manifest = Manifest.load(manifest_path) or Manifest(
        source_id=src.repo.replace("/", "_"), repo=src.repo, ref=src.ref)

    pre = Report()
    if not dry_run:
        reconcile_inflight(manifest, panel, pre)
        manifest.save(manifest_path)

    selected = PL.select(tree, include, exclude)
    plan = PL.diff(selected, manifest)
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
