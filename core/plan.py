"""Diff a remote tree against the manifest. Only `add` is ever written."""

from dataclasses import dataclass, field
from functools import lru_cache
import re
from typing import Dict, List, Mapping, Optional, Sequence

from .manifest import Manifest, INFLIGHT


@lru_cache(maxsize=256)
def _compile(pattern: str):
    """Git-style globs: '*' stops at '/', '**' crosses it, '**/' matches zero
    or more directories. fnmatch gets all three wrong for our purposes."""
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
    add: List[str] = field(default_factory=list)         # not in manifest -> upload
    change: List[str] = field(default_factory=list)      # blob differs -> Phase 2
    orphan: List[str] = field(default_factory=list)      # gone upstream -> cannot delete
    unverified: List[str] = field(default_factory=list)  # adopted with unknown blob
    inflight: List[str] = field(default_factory=list)    # interrupted, needs reconcile

    @property
    def is_empty(self) -> bool:
        return not (self.add or self.change or self.orphan
                    or self.unverified or self.inflight)


def diff(remote: Mapping[str, str], manifest: Optional[Manifest]) -> Plan:
    """`remote` must already be filtered by select(), or excluded files read
    as orphans."""
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
