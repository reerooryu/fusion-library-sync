"""The Data Panel, behind an interface.

Two implementations:
  FusionDataPanel - the real thing; imports adsk lazily so this module stays
                    importable (and testable) outside Fusion.
  FakeDataPanel   - an in-memory stand-in that reproduces Fusion's DANGEROUS
                    behaviour: uploading a same-named file into a folder
                    creates a second file on a new lineage, silently. Verified
                    against a live project on 6 Sep 2026.

The fake models the bug on purpose. A test suite against a well-behaved fake
would prove nothing about the invariant we actually have to hold.
"""

from dataclasses import dataclass
from typing import Dict, List, Optional, Protocol, Sequence, Tuple
import itertools


@dataclass(frozen=True)
class PlacedFile:
    name: str                  # display name in the Data Panel
    lineage: str               # urn:adsk.wipprod:dm.lineage:...
    version: int = 1


class UploadFailed(RuntimeError):
    pass


class DataPanel(Protocol):
    def ensure_folder(self, folders: Sequence[str]) -> str: ...
    def upload(self, folder_id: str, local_path: str, name: str,
               on_wait=None) -> PlacedFile: ...
    def list_folder(self, folder_id: str) -> List[PlacedFile]: ...
    def find_by_name(self, folder_id: str, name: str) -> List[PlacedFile]: ...
    def scan(self) -> Dict[Tuple[str, ...], List[PlacedFile]]: ...


# --------------------------------------------------------------------------
class FakeDataPanel:
    """In-memory Data Panel with Fusion's real duplicate behaviour."""

    def __init__(self, fail_on: Optional[Sequence[str]] = None,
                 crash_after: Optional[int] = None):
        self.folders: Dict[Tuple[str, ...], str] = {(): "folder:root"}
        self.contents: Dict[str, List[PlacedFile]] = {"folder:root": []}
        self._ids = itertools.count(1)
        self.uploads: List[str] = []
        self.fail_on = set(fail_on or ())
        self.crash_after = crash_after

    def ensure_folder(self, folders: Sequence[str]) -> str:
        key: Tuple[str, ...] = ()
        for seg in folders:
            key = key + (seg,)
            if key not in self.folders:
                fid = f"folder:{next(self._ids)}"
                self.folders[key] = fid
                self.contents[fid] = []
        return self.folders[key]

    def upload(self, folder_id: str, local_path: str, name: str,
               on_wait=None) -> PlacedFile:
        if name in self.fail_on:
            raise UploadFailed(f"simulated failure for {name!r}")
        if self.crash_after is not None and len(self.uploads) >= self.crash_after:
            raise KeyboardInterrupt("simulated interruption")

        self.uploads.append(name)
        # Fusion does NOT dedupe, warn, or rename. Neither do we.
        pf = PlacedFile(name=name, lineage=f"urn:adsk.wipprod:dm.lineage:{next(self._ids)}")
        self.contents[folder_id].append(pf)
        return pf

    def list_folder(self, folder_id: str) -> List[PlacedFile]:
        return list(self.contents.get(folder_id, []))

    def find_by_name(self, folder_id: str, name: str) -> List[PlacedFile]:
        return [f for f in self.contents.get(folder_id, []) if f.name == name]

    def scan(self) -> Dict[Tuple[str, ...], List[PlacedFile]]:
        by_path = {v: k for k, v in self.folders.items()}
        return {by_path[fid]: list(files) for fid, files in self.contents.items() if files}

    # -- test helpers --
    @property
    def total_files(self) -> int:
        return sum(len(v) for v in self.contents.values())

    def duplicates(self) -> Dict[str, int]:
        """Any folder holding two files with one name. Must always be empty."""
        dupes: Dict[str, int] = {}
        for fid, files in self.contents.items():
            counts: Dict[str, int] = {}
            for f in files:
                counts[f.name] = counts.get(f.name, 0) + 1
            for name, n in counts.items():
                if n > 1:
                    dupes[f"{fid}/{name}"] = n
        return dupes


# --------------------------------------------------------------------------
class FusionDataPanel:
    """Real Data Panel. Only constructed inside Fusion."""

    def __init__(self, project, root_folder_path: Sequence[str] = ()):
        import adsk.core  # noqa: F401  - fails loudly outside Fusion
        self._project = project
        self._cache: Dict[Tuple[str, ...], object] = {}
        self._root = self._resolve(project.rootFolder, root_folder_path)

    def _resolve(self, folder, parts: Sequence[str]):
        for seg in parts:
            nxt = None
            for i in range(folder.dataFolders.count):
                f = folder.dataFolders.item(i)
                if f.name == seg:
                    nxt = f
                    break
            folder = nxt or folder.dataFolders.add(seg)
        return folder

    def ensure_folder(self, folders: Sequence[str]):
        key = tuple(folders)
        if key in self._cache:
            return self._cache[key]
        folder = self._resolve(self._root, folders)
        self._cache[key] = folder
        return folder

    def upload(self, folder, local_path: str, name: str,
               on_wait=None) -> PlacedFile:
        import adsk.core
        future = folder.uploadFile(local_path)
        # Asynchronous: 0 = Processing, 1 = Finished, 2 = Failed.
        import time
        started = time.time()
        deadline = started + 300
        while time.time() < deadline:
            state = future.uploadState
            if state != 0:
                break
            if on_wait:
                on_wait(time.time() - started)
            adsk.doEvents()
            time.sleep(0.4)
        else:
            raise UploadFailed(f"timed out uploading {name!r}")

        if future.uploadState != 1:
            raise UploadFailed(f"upload failed for {name!r} (state {future.uploadState})")

        df = future.dataFile
        if df is None:
            raise UploadFailed(f"no DataFile returned for {name!r}")
        return PlacedFile(name=df.name, lineage=df.id,
                          version=getattr(df, "versionNumber", 1) or 1)

    def list_folder(self, folder) -> List[PlacedFile]:
        try:
            folder.refresh()
        except Exception:
            pass
        out = []
        for i in range(folder.dataFiles.count):
            f = folder.dataFiles.item(i)
            out.append(PlacedFile(name=f.name, lineage=f.id,
                                  version=getattr(f, "versionNumber", 1) or 1))
        return out

    def find_by_name(self, folder, name: str) -> List[PlacedFile]:
        return [f for f in self.list_folder(folder) if f.name == name]

    def scan(self) -> Dict[Tuple[str, ...], List[PlacedFile]]:
        """Walk the whole subtree. Used by adopt and by inflight reconcile."""
        found: Dict[Tuple[str, ...], List[PlacedFile]] = {}

        def walk(folder, path: Tuple[str, ...]):
            files = self.list_folder(folder)
            if files:
                found[path] = files
            for i in range(folder.dataFolders.count):
                sub = folder.dataFolders.item(i)
                walk(sub, path + (sub.name,))

        walk(self._root, ())
        return found
