"""Manifest: have I placed this path, and which cloud file is it?

Identity is the lineage URN, never the display name - Fusion will hold three
files with one name in a folder, each on its own lineage, without complaint.
"""

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Dict, Iterable, Optional
import json
import os
import tempfile

SCHEMA = 1

# Entry states
PLACED = "placed"      # uploaded by us, lineage known, blob known
ADOPTED = "adopted"    # pre-existing file we claimed; blob from the release
                       # the user named, or None if they did not know
INFLIGHT = "inflight"  # upload started, completion unconfirmed


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Entry:
    blob: Optional[str] = None       # git blob SHA; None => content unverified
    lineage: Optional[str] = None    # urn:adsk.wipprod:dm.lineage:...
    version: Optional[int] = None
    state: str = INFLIGHT
    at: str = field(default_factory=_now)
    placed_name: Optional[str] = None  # set only when it differs from the map


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

    def paths(self) -> Iterable[str]:
        return self.files.keys()

    def in_state(self, state: str):
        return {p: e for p, e in self.files.items() if e.state == state}

    # ---- mutation ------------------------------------------------------
    def mark_inflight(self, repo_path: str, placed_name: Optional[str] = None) -> None:
        """Written BEFORE the upload, so a crash mid-upload is visible."""
        self.files[repo_path] = Entry(
            state=INFLIGHT, at=_now(), placed_name=placed_name
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

    # ---- persistence ---------------------------------------------------
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
        """Atomic, and called after every file - a crash must leave a manifest
        that matches the cloud, or the next run re-uploads."""
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
    """Claim an existing library without uploading anything.

    Adopting against the named release rather than HEAD is the point: a user on
    v2.0.3 recorded with today's hashes would look current and never update.
    release_known=False records blob=None rather than guessing.
    """
    m = Manifest(source_id=source_id, repo=repo, ref=ref)
    for repo_path, lineage in local.items():
        blob = tree_at_release.get(repo_path) if release_known else None
        m.files[repo_path] = Entry(
            blob=blob, lineage=lineage, version=None,
            state=ADOPTED, at=_now(),
        )
    return m
