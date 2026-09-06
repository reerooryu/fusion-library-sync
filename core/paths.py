"""Repo path -> Data Panel location.

Names are preserved verbatim and Unicode is never normalised: folding U+201D
to a straight quote would make the manifest key stop matching the tree, so
every later sync would see a phantom change. Any alteration is reported - a
silent rename is a future duplicate.
"""

from dataclasses import dataclass
from typing import List, Sequence, Tuple
import posixpath
import unicodedata

# Conservative ceiling until Fusion's real limit is measured (spec section 11).
MAX_SEGMENT = 255
MAX_PATH = 400

# Characters no filesystem or Data Panel will take in a single segment.
_ILLEGAL = set('/\\\x00')


@dataclass(frozen=True)
class PanelPath:
    """Where a repo file goes in the Data Panel."""
    repo_path: str                      # canonical key, extension included
    folders: Tuple[str, ...]            # folder chain below the source root
    name: str                           # display name, extension stripped
    alterations: Tuple[str, ...] = ()   # non-empty => must appear in the report

    @property
    def altered(self) -> bool:
        return bool(self.alterations)

    @property
    def panel_path(self) -> str:
        return posixpath.join(*self.folders, self.name) if self.folders else self.name


class PathError(ValueError):
    """The path cannot be represented in the Data Panel at all."""


def _clean_segment(seg: str, where: str, notes: List[str]) -> str:
    """Only alterations Fusion actually forces."""
    original = seg
    if any(ch in _ILLEGAL for ch in seg):
        raise PathError(f"{where}: illegal character in segment {seg!r}")

    if any(unicodedata.category(ch) == 'Cc' for ch in seg):
        seg = ''.join(ch for ch in seg if unicodedata.category(ch) != 'Cc')
        notes.append(f"{where}: stripped control characters")

    # Some hosts drop these silently; do it ourselves so the manifest is honest.
    stripped = seg.rstrip(' .')
    if stripped != seg:
        seg = stripped
        notes.append(f"{where}: trimmed trailing space/period")

    if not seg:
        raise PathError(f"{where}: segment empty after cleaning ({original!r})")
    if len(seg) > MAX_SEGMENT:
        raise PathError(f"{where}: segment exceeds {MAX_SEGMENT} chars")

    return seg


def map_path(repo_path: str, subpath: str = "") -> PanelPath:
    """`subpath` is stripped from the front so a source can sync a subtree."""
    if not repo_path or repo_path.endswith('/'):
        raise PathError(f"not a file path: {repo_path!r}")

    rel = repo_path
    if subpath:
        prefix = subpath.strip('/') + '/'
        if not rel.startswith(prefix):
            raise PathError(f"{repo_path!r} is outside subpath {subpath!r}")
        rel = rel[len(prefix):]

    parts = [p for p in rel.split('/') if p not in ('', '.')]
    if any(p == '..' for p in parts):
        raise PathError(f"path traversal in {repo_path!r}")
    if not parts:
        raise PathError(f"empty path after subpath removal: {repo_path!r}")

    notes: List[str] = []
    *folder_parts, filename = parts

    folders = tuple(
        _clean_segment(p, f"folder {i + 1}", notes) for i, p in enumerate(folder_parts)
    )

    # Fusion supplies its own extension; the manifest still keys on repo_path.
    stem, dot, _ext = filename.rpartition('.')
    display = stem if dot else filename
    display = _clean_segment(display, "filename", notes)

    if len(posixpath.join(*folders, display) if folders else display) > MAX_PATH:
        raise PathError(f"path exceeds {MAX_PATH} chars: {repo_path!r}")

    return PanelPath(
        repo_path=repo_path,
        folders=folders,
        name=display,
        alterations=tuple(notes),
    )


def map_all(repo_paths: Sequence[str], subpath: str = ""):
    """Returns (mapped, errors, collisions). A collision - two repo files
    landing on one Data Panel name - must block the sync: that is how a
    library silently loses a part."""
    mapped: List[PanelPath] = []
    errors: List[Tuple[str, str]] = []
    seen: dict = {}

    for p in repo_paths:
        try:
            pp = map_path(p, subpath)
        except PathError as exc:
            errors.append((p, str(exc)))
            continue
        mapped.append(pp)
        seen.setdefault(pp.panel_path, []).append(p)

    collisions = {k: v for k, v in seen.items() if len(v) > 1}
    return mapped, errors, collisions
