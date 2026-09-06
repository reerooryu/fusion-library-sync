"""Diff a remote tree against the manifest.

Four sets, one write path. Only `add` is acted on in Phase 1; everything
else is reported so a human decides.
"""

from dataclasses import dataclass, field
from fnmatch import fnmatch
from typing import Dict, List, Mapping, Optional, Sequence

from .manifest import Manifest, INFLIGHT


def _matches(path: str, patterns: Sequence[str]) -> bool:
    for pat in patterns:
        # '**/*.f3d' should match a file at the root too.
        if fnmatch(path, pat):
            return True
        if pat.startswith("**/") and fnmatch(path, pat[3:]):
            return True
    return False


def select(tree: Mapping[str, str],
           include: Sequence[str] = ("**/*",),
           exclude: Sequence[str] = ()) -> Dict[str, str]:
    """Filter a {repo_path: blob} tree by include/exclude globs."""
    out = {}
    for path, blob in tree.items():
        if include and not _matches(path, include):
            continue
        if exclude and _matches(path, exclude):
            continue
        out[path] = blob
    return out


@dataclass
class Plan:
    add: List[str] = field(default_factory=list)         # not in manifest -> upload
    change: List[str] = field(default_factory=list)      # blob differs -> Phase 2
    orphan: List[str] = field(default_factory=list)      # gone upstream -> cannot delete
    unverified: List[str] = field(default_factory=list)  # adopted with unknown blob
    inflight: List[str] = field(default_factory=list)    # interrupted, needs reconcile

    @property
    def actionable(self) -> List[str]:
        """The only paths Phase 1 will write."""
        return self.add

    @property
    def is_empty(self) -> bool:
        return not (self.add or self.change or self.orphan
                    or self.unverified or self.inflight)

    def summary(self) -> str:
        return (f"+{len(self.add)} add  "
                f"~{len(self.change)} changed  "
                f"-{len(self.orphan)} gone  "
                f"?{len(self.unverified)} unverified  "
                f"!{len(self.inflight)} inflight")


def diff(remote: Mapping[str, str], manifest: Optional[Manifest]) -> Plan:
    """Compare a filtered remote tree against what we believe we placed.

    `remote` must already be filtered by select(); diffing an unfiltered tree
    against a filtered manifest would report every excluded file as an orphan.
    """
    plan = Plan()
    files = manifest.files if manifest else {}

    for path in sorted(remote):
        entry = files.get(path)
        if entry is None:
            plan.add.append(path)
        elif entry.state == INFLIGHT:
            plan.inflight.append(path)
        elif entry.blob is None:
            plan.unverified.append(path)
        elif entry.blob != remote[path]:
            plan.change.append(path)

    for path in sorted(files):
        if path not in remote:
            plan.orphan.append(path)

    return plan
