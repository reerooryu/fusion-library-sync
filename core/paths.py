"""Repo path -> Data Panel location.

Rules (spec section 06):
  1. Preserve names verbatim wherever Fusion accepts them.
  2. Never normalise Unicode - folding U+201D to '"' would make the manifest
     path and the repo path disagree, and every later sync would see a
     phantom change.
  3. Strip the extension for the display name; key the manifest on the repo
     path *including* extension.
  4. If a name must be altered to be accepted, say so. A silent rename is a
     future duplicate.
"""

from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple
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
    """Only alterations Fusion actually forces. Everything else is preserved."""
    original = seg

    if any(ch in _ILLEGAL for ch in seg):
        raise PathError(f"{where}: illegal character in segment {seg!r}")

    # Control characters cannot survive a round trip.
    if any(unicodedata.category(ch) == 'Cc' for ch in seg):
        seg = ''.join(ch for ch in seg if unicodedata.category(ch) != 'Cc')
        notes.append(f"{where}: stripped control characters")

    # Trailing dots and spaces are silently dropped by some hosts; drop them
    # ourselves so the manifest records what actually landed.
    stripped = seg.rstrip(' .')
    if stripped != seg:
        seg = stripped
        notes.append(f"{where}: trimmed trailing space/period")

    # A leading space is preserved by Fusion but confuses sorting; leave it,
    # only flag it. NOTE: a leading '!' is legal and common in this corpus.
    if original.startswith(' '):
        notes.append(f"{where}: leading space kept verbatim")

    if not seg:
        raise PathError(f"{where}: segment empty after cleaning ({original!r})")
    if len(seg) > MAX_SEGMENT:
        raise PathError(f"{where}: segment exceeds {MAX_SEGMENT} chars")

    return seg


def map_path(repo_path: str, subpath: str = "") -> PanelPath:
    """Map a repository path to its Data Panel location.

    `subpath` is stripped from the front so a source can sync a subtree.
    """
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

    # Extension goes; Fusion supplies its own. The manifest still keys on
    # repo_path, so this is display-only.
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
    """Map many paths, collecting failures rather than raising on the first.

    Returns (mapped, errors, collisions):
      mapped     - list[PanelPath]
      errors     - list[(repo_path, message)]
      collisions - dict[panel_path, list[repo_path]] where >1 file would land
                   in the same place. These MUST block a sync: two repo files
                   mapping to one Data Panel name is exactly how a library
                   silently loses a part.
    """
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
