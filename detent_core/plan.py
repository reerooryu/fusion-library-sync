"""Compare the repo tree with the manifest. Only `add` and `missing` are uploaded."""

from dataclasses import dataclass, field
from functools import lru_cache
import re
from typing import Collection, Dict, List, Mapping, Optional, Sequence

from .manifest import Manifest, INFLIGHT


@lru_cache(maxsize=256)
def _compile(pattern: str):
    """Git-style globs: '*' stops at '/', '**' crosses it, '**/' matches zero
    or more folders. fnmatch gets these wrong."""
    out, i, n = [], 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "*":
            if pattern[i:i + 3] == "**/":
                out.append("(?:.*/)?")
                i += 3
                continue
            if pattern[i:i + 2] == "**":
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("".join(out) + r"\Z")


def _matches(path: str, patterns: Sequence[str]) -> bool:
    return any(_compile(pat).match(path) for pat in patterns)


def select(tree: Mapping[str, str],
           include: Sequence[str] = ("**/*",),
           exclude: Sequence[str] = ()) -> Dict[str, str]:
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
    add: List[str] = field(default_factory=list)         # not in manifest: upload
    change: List[str] = field(default_factory=list)      # blob differs: not handled yet
    orphan: List[str] = field(default_factory=list)      # removed upstream: can't delete
    unverified: List[str] = field(default_factory=list)  # adopted without a blob
    inflight: List[str] = field(default_factory=list)    # upload not confirmed yet
    missing: List[str] = field(default_factory=list)     # placed, but gone from the panel
    conflict: List[str] = field(default_factory=list)    # gone, and its name is taken
    # Commit the plan was built from, so the upload matches the preview.
    commit: Optional[str] = None

    @property
    def is_empty(self) -> bool:
        return not (self.add or self.change or self.orphan or self.unverified
                    or self.inflight or self.missing or self.conflict)

    @property
    def to_place(self) -> List[str]:
        """Everything this run may upload: new files and missing ones."""
        return self.add + self.missing


def diff(remote: Mapping[str, str], manifest: Optional[Manifest],
         missing: Collection[str] = ()) -> Plan:
    """`remote` must already be filtered by select(), or excluded files look
    like they were removed upstream.

    `missing` wins over every other category: a file that isn't there can't be
    up to date or changed.
    """
    plan = Plan()
    files = manifest.files if manifest else {}

    for path in sorted(remote):
        entry = files.get(path)
        if entry is None:
            plan.add.append(path)
        elif path in missing:
            plan.missing.append(path)
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
