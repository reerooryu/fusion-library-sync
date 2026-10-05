"""The Data Panel, behind an interface.

FusionDataPanel is the real one; it imports adsk only when created, so this
module works outside Fusion. FakeDataPanel is for tests, and copies two things
Fusion does:

- It keeps two files with the same name in one folder without a warning
  (checked 6 Sep 2026).
- While a file is still uploading, its id is a temporary local path, not its
  Fusion ID. The real ID appears when the upload finishes (seen 23 Sep 2026).
"""

from dataclasses import dataclass
from typing import Dict, List, Optional, Protocol, Sequence, Tuple
import itertools
import time


@dataclass(frozen=True)
class PlacedFile:
    name: str                  # display name in the Data Panel
    lineage: str               # urn:adsk.wipprod:dm.lineage:...
    version: int = 1


class UploadFailed(RuntimeError):
    pass


class PanelError(RuntimeError):
    """Fusion wouldn't answer a Data Panel request, even after retries."""


def is_fusion_id(value) -> bool:
    """True for a real Fusion ID. While a file is uploading Fusion reports a
    local file path instead, which changes when the upload finishes."""
    return isinstance(value, str) and value.startswith("urn:")


class DataPanel(Protocol):
    # No blocking upload. Waiting on an upload cost 228 s per file, so uploads
    # are started with begin_upload and confirmed later.
    def ensure_folder(self, folders: Sequence[str]) -> str: ...
    def begin_upload(self, folder_id: str, local_path: str, name: str): ...
    def poll_upload(self, handle) -> Optional[PlacedFile]: ...
    def list_folder(self, folder_id: str) -> List[PlacedFile]: ...
    def find_by_name(self, folder_id: str, name: str) -> List[PlacedFile]: ...
    def scan(self) -> Dict[Tuple[str, ...], List[PlacedFile]]: ...


# --------------------------------------------------------------------------
class FakeDataPanel:
    """In-memory Data Panel that allows duplicate names, like Fusion."""

    def __init__(self, fail_on: Optional[Sequence[str]] = None,
                 crash_after: Optional[int] = None,
                 deferred: bool = False, temp_ids: int = 0,
                 refuse_folders: Sequence[Tuple[str, ...]] = ()):
        self.deferred = deferred
        # Folders Fusion won't open, like a Data Panel call that keeps failing.
        self.refuse_folders = {tuple(f) for f in refuse_folders}
        self.folder_calls: List[Tuple[str, ...]] = []
        # A new file shows a temporary local path as its id for its first
        # `temp_ids` listings, like Fusion while an upload is in progress.
        self.temp_ids = temp_ids
        self._seen: Dict[str, int] = {}
        self.folders: Dict[Tuple[str, ...], str] = {(): "folder:root"}
        self.contents: Dict[str, List[PlacedFile]] = {"folder:root": []}
        self._ids = itertools.count(1)
        self.uploads: List[str] = []
        self.fail_on = set(fail_on or ())
        self.crash_after = crash_after

    def ensure_folder(self, folders: Sequence[str]) -> str:
        self.folder_calls.append(tuple(folders))
        if tuple(folders) in self.refuse_folders:
            raise PanelError(f"Fusion wouldn't open {'/'.join(folders)!r}: "
                             "InternalValidationError")
        key: Tuple[str, ...] = ()
        for seg in folders:
            key = key + (seg,)
            if key not in self.folders:
                fid = f"folder:{next(self._ids)}"
                self.folders[key] = fid
                self.contents[fid] = []
        return self.folders[key]

    def upload(self, folder_id: str, local_path: str, name: str) -> PlacedFile:
        """Place a file at once. For tests setting up files; sync never calls it."""
        if name in self.fail_on:
            raise UploadFailed(f"simulated failure for {name!r}")
        if self.crash_after is not None and len(self.uploads) >= self.crash_after:
            raise KeyboardInterrupt("simulated interruption")

        self.uploads.append(name)
        # Like Fusion: no check for an existing file with this name.
        pf = PlacedFile(name=name, lineage=f"urn:adsk.wipprod:dm.lineage:{next(self._ids)}")
        self.contents[folder_id].append(pf)
        return pf

    def begin_upload(self, folder_id: str, local_path: str, name: str):
        """Start an upload and return a handle."""
        placed = self.upload(folder_id, local_path, name)
        return {"placed": placed, "polls": 0}

    def poll_upload(self, handle) -> Optional[PlacedFile]:
        """None while still uploading. With deferred=True, takes a few polls."""
        handle["polls"] += 1
        if self.deferred and handle["polls"] < 3:
            return None
        placed = handle["placed"]
        if placed is not None and self._seen.get(placed.lineage, 0) < self.temp_ids:
            return None                  # still uploading, like its listing says
        return placed

    def _as_seen(self, pf: PlacedFile) -> PlacedFile:
        """The file as a listing shows it right now."""
        n = self._seen.get(pf.lineage, 0)
        self._seen[pf.lineage] = n + 1
        if n < self.temp_ids:
            tmp = "/Users/tester/Library/Application Support/Autodesk/Q/" + pf.lineage.rsplit(":", 1)[1] + ".f3d"
            return PlacedFile(name=pf.name, lineage=tmp, version=pf.version)
        return pf

    def list_folder(self, folder_id: str) -> List[PlacedFile]:
        return [self._as_seen(f) for f in self.contents.get(folder_id, [])]

    def find_by_name(self, folder_id: str, name: str) -> List[PlacedFile]:
        return [f for f in self.list_folder(folder_id) if f.name == name]

    def scan(self) -> Dict[Tuple[str, ...], List[PlacedFile]]:
        by_path = {v: k for k, v in self.folders.items()}
        return {by_path[fid]: self.list_folder(fid)
                for fid, files in self.contents.items() if files}

    # -- test helpers --
    @property
    def total_files(self) -> int:
        return sum(len(v) for v in self.contents.values())

    def duplicates(self) -> Dict[str, int]:
        """Folders holding two files with the same name. Should always be empty."""
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
    """The real Data Panel. Only works inside Fusion."""

    # Fusion's data calls sometimes fail for a moment (seen 5 Oct 2026:
    # "InternalValidationError : status.isOk() && folders" from
    # dataFolders.count). Wait this long between tries, in seconds.
    RETRY_WAITS = (0.5, 1.0, 2.0, 4.0)

    def __init__(self, project, root_folder_path: Sequence[str] = ()):
        import adsk.core  # noqa: F401  - fails outside Fusion
        self._project = project
        self._root_path = tuple(root_folder_path)
        self._cache: Dict[Tuple[str, ...], object] = {}
        try:
            self._root = self._retry(
                lambda: self._resolve(project.rootFolder, self._root_path))
        except Exception as exc:                      # noqa: BLE001
            where = "/".join(self._root_path) or "the project"
            raise PanelError(f"Fusion wouldn't open {where!r}: {exc}") from exc

    def _pause(self, seconds: float) -> None:
        """Wait without freezing Fusion's window."""
        end = time.time() + seconds
        while time.time() < end:
            try:
                import adsk
                adsk.doEvents()
            except Exception:                         # noqa: BLE001
                pass
            time.sleep(0.05)

    def _retry(self, call):
        """Run a Fusion call. If Fusion reports an error, wait and try again."""
        waits = list(self.RETRY_WAITS)
        while True:
            try:
                return call()
            except Exception:                         # noqa: BLE001
                if not waits:
                    raise
                self._pause(waits.pop(0))

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
        try:
            folder = self._retry(lambda: self._resolve(self._root, key))
        except Exception:                             # noqa: BLE001
            # The folder objects we kept may have gone stale. Start again
            # from the project.
            self._cache.clear()
            try:
                self._root = self._resolve(self._project.rootFolder,
                                           self._root_path)
                folder = self._resolve(self._root, key)
            except Exception as exc:                  # noqa: BLE001
                where = "/".join(self._root_path + key) or "the project"
                raise PanelError(
                    f"Fusion wouldn't open {where!r}: {exc}") from exc
        self._cache[key] = folder
        return folder

    def begin_upload(self, folder, local_path: str, name: str):
        """Start the upload and return straight away. Uploads run in parallel."""
        try:
            return {"future": folder.uploadFile(local_path), "name": name}
        except Exception as exc:                      # noqa: BLE001
            raise UploadFailed(f"{name}: Fusion refused the upload: {exc}")

    def poll_upload(self, handle) -> Optional[PlacedFile]:
        """None while still uploading. Raises UploadFailed if it failed.

        uploadState: 0 processing, 1 finished, 2 failed.
        """
        future = handle["future"]
        try:
            state = future.uploadState
        except Exception as exc:                      # noqa: BLE001
            raise UploadFailed(f"{handle['name']}: uploadState raised: {exc}")

        if state == 0:
            return None
        if state != 1:
            raise UploadFailed(f"{handle['name']}: upload state {state}")

        df = future.dataFile
        if df is None:
            raise UploadFailed(f"{handle['name']}: finished with no DataFile")
        return PlacedFile(name=df.name, lineage=df.id,
                          version=getattr(df, "versionNumber", 1) or 1)

    def list_folder(self, folder) -> List[PlacedFile]:
        # DataFolder has no refresh(); dataFiles is always current.
        def read():
            out = []
            for i in range(folder.dataFiles.count):
                f = folder.dataFiles.item(i)
                out.append(PlacedFile(name=f.name, lineage=f.id,
                                      version=getattr(f, "versionNumber", 1) or 1))
            return out
        return self._retry(read)

    def find_by_name(self, folder, name: str) -> List[PlacedFile]:
        return [f for f in self.list_folder(folder) if f.name == name]

    def scan(self) -> Dict[Tuple[str, ...], List[PlacedFile]]:
        """Every file in every folder under the root."""
        found: Dict[Tuple[str, ...], List[PlacedFile]] = {}

        def subfolders(folder):
            return [folder.dataFolders.item(i)
                    for i in range(folder.dataFolders.count)]

        def walk(folder, path: Tuple[str, ...]):
            files = self.list_folder(folder)
            if files:
                found[path] = files
            for sub in self._retry(lambda: subfolders(folder)):
                walk(sub, path + (sub.name,))

        walk(self._root, ())
        return found
