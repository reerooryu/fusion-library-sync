"""Manifest: which files Detent has placed, and which Fusion file each became.

Files are tracked by lineage URN, not name, because Fusion allows duplicate
names.
"""

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Dict, Optional
import json
import os
import tempfile

SCHEMA = 1

# Entry states
PLACED = "placed"      # uploaded by Detent; lineage and blob known
ADOPTED = "adopted"    # already there, claimed by Adopt; blob from the named
                       # release, or None if no release was given
INFLIGHT = "inflight"  # upload started, not yet confirmed


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Entry:
    blob: Optional[str] = None       # git blob SHA; None means unverified
    lineage: Optional[str] = None    # urn:adsk.wipprod:dm.lineage:...
    version: Optional[int] = None
    state: str = INFLIGHT
    at: str = field(default_factory=_now)
    placed_name: Optional[str] = None  # only set if it differs from the mapped name


@dataclass
class Manifest:
    source_id: str
    repo: str
    ref: str = "main"
    synced_commit: Optional[str] = None
    synced_at: Optional[str] = None
    schema: int = SCHEMA
    files: Dict[str, Entry] = field(default_factory=dict)

    # ---- lookups -------------------------------------------------------
    def __contains__(self, repo_path: str) -> bool:
        return repo_path in self.files

    def get(self, repo_path: str) -> Optional[Entry]:
        return self.files.get(repo_path)

    def in_state(self, state: str):
        return {p: e for p, e in self.files.items() if e.state == state}

    # ---- changes -------------------------------------------------------
    def mark_inflight(self, repo_path: str, placed_name: Optional[str] = None,
                      blob: Optional[str] = None) -> None:
        """Called before an upload, so a crash leaves a record. `blob` is the
        version being uploaded. The upload may be confirmed on a later run,
        after a newer release, so the record must keep this one."""
        self.files[repo_path] = Entry(
            blob=blob, state=INFLIGHT, at=_now(), placed_name=placed_name
        )

    def record(self, repo_path: str, blob: str, lineage: str,
               version: int = 1, state: str = PLACED,
               placed_name: Optional[str] = None) -> None:
        self.files[repo_path] = Entry(
            blob=blob, lineage=lineage, version=version,
            state=state, at=_now(), placed_name=placed_name,
        )

    def drop(self, repo_path: str) -> None:
        self.files.pop(repo_path, None)

    def stamp(self, ref: str, commit: Optional[str]) -> None:
        """Record the ref, commit and time of the last sync. Called on every
        sync, including ones with nothing to upload."""
        self.ref = ref
        self.synced_commit = commit
        self.synced_at = _now()

    # ---- saving and loading --------------------------------------------
    def to_dict(self) -> dict:
        return {
            "schema": self.schema,
            "source_id": self.source_id,
            "repo": self.repo,
            "ref": self.ref,
            "synced_commit": self.synced_commit,
            "synced_at": self.synced_at,
            "files": {p: asdict(e) for p, e in sorted(self.files.items())},
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Manifest":
        got = d.get("schema", 0)
        if got != SCHEMA:
            raise ValueError(f"manifest schema {got}, expected {SCHEMA}")
        m = cls(
            source_id=d["source_id"], repo=d["repo"], ref=d.get("ref", "main"),
            synced_commit=d.get("synced_commit"), synced_at=d.get("synced_at"),
        )
        for p, e in (d.get("files") or {}).items():
            m.files[p] = Entry(**e)
        return m

    def save(self, path: str) -> None:
        """Atomic write. Called after every file, so a crash never leaves the
        manifest behind what's in the cloud."""
        directory = os.path.dirname(os.path.abspath(path)) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self.to_dict(), fh, indent=2, ensure_ascii=False)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except Exception:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    @classmethod
    def load(cls, path: str) -> Optional["Manifest"]:
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh))


def adopt(source_id: str, repo: str, ref: str,
          tree_at_release: Dict[str, str],
          local: Dict[str, str],
          release_known: bool = True) -> "Manifest":
    """Build a manifest for a library the user already has. Uploads nothing.

    Blobs come from the release the user named, not the latest one. Otherwise
    an old library would look up to date. release_known=False records no blob.
    """
    m = Manifest(source_id=source_id, repo=repo, ref=ref)
    for repo_path, lineage in local.items():
        blob = tree_at_release.get(repo_path) if release_known else None
        m.files[repo_path] = Entry(
            blob=blob, lineage=lineage, version=None,
            state=ADOPTED, at=_now(),
        )
    return m
