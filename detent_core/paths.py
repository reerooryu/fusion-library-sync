"""Repo path to Data Panel folder and file name.

Names are kept exactly, including Unicode. Normalising a character like
U+201D would make the manifest stop matching the repo. Any change to a name is
reported, because an unnoticed rename becomes a duplicate later.
"""

from dataclasses import dataclass
from typing import List, Sequence, Tuple
import posixpath
import unicodedata

# Safe limits until Fusion's real ones are measured.
MAX_SEGMENT = 255
MAX_PATH = 400

# Characters no file system allows in a name.
_ILLEGAL = set('/\\\x00')


@dataclass(frozen=True)
class PanelPath:
    """Where a repo file goes in the Data Panel."""
    repo_path: str                      # manifest key, with extension
    folders: Tuple[str, ...]            # folders below the source root
    name: str                           # display name, without extension
    alterations: Tuple[str, ...] = ()   # changes made to the name, if any

    @property
    def altered(self) -> bool:
        return bool(self.alterations)

    @property
    def panel_path(self) -> str:
        return posixpath.join(*self.folders, self.name) if self.folders else self.name


class PathError(ValueError):
    """The path can't be used in the Data Panel."""


def _clean_segment(seg: str, where: str, notes: List[str]) -> str:
    """Only make changes Fusion would force anyway."""
    original = seg
    if any(ch in _ILLEGAL for ch in seg):
        raise PathError(f"{where}: illegal character in segment {seg!r}")

    if any(unicodedata.category(ch) == 'Cc' for ch in seg):
        seg = ''.join(ch for ch in seg if unicodedata.category(ch) != 'Cc')
        notes.append(f"{where}: stripped control characters")

    # Some systems drop these silently; do it here so the manifest matches.
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
    """`subpath` is removed from the front, to sync part of a repo."""
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

    # Fusion adds its own extension. The manifest still uses the full repo path.
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
    """Returns (mapped, errors, collisions). A collision means two repo files
    would get the same Data Panel name, and must block the sync."""
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
