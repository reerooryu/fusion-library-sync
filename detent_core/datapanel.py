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
    def peek_folder(self, folders: Sequence[str]): ...
    def wait(self, seconds: float) -> None: ...
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
                 refuse_folders: Sequence[Tuple[str, ...]] = (),
                 root_exists: bool = True):
        self.deferred = deferred
        # False: the library folder itself isn't in the project (yet).
        self.root_exists = root_exists
        # Folders Fusion won't open, like a Data Panel call that keeps failing.
        self.refuse_folders = {tuple(f) for f in refuse_folders}
        self.folder_calls: List[Tuple[str, ...]] = []
        self.listings: List[str] = []         # every folder listing, in order
        self.scans = 0                        # full-library scans
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
        self.root_exists = True
        key: Tuple[str, ...] = ()
        for seg in folders:
            key = key + (seg,)
            if key not in self.folders:
                fid = f"folder:{next(self._ids)}"
                self.folders[key] = fid
                self.contents[fid] = []
        return self.folders[key]

    def peek_folder(self, folders: Sequence[str]) -> Optional[str]:
        """The folder, or None if it doesn't exist. Creates nothing."""
        if tuple(folders) in self.refuse_folders:
            raise PanelError(f"Fusion wouldn't open {'/'.join(folders)!r}: "
                             "InternalValidationError")
        if not self.root_exists:
            return None
        return self.folders.get(tuple(folders))

    def wait(self, seconds: float) -> None:
        time.sleep(seconds)

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
        self.listings.append(folder_id)
        return [self._as_seen(f) for f in self.contents.get(folder_id, [])]

    def find_by_name(self, folder_id: str, name: str) -> List[PlacedFile]:
        return [f for f in self.list_folder(folder_id) if f.name == name]

    def scan(self, on_folder=None) -> Dict[Tuple[str, ...], List[PlacedFile]]:
        self.scans += 1
        by_path = {v: k for k, v in self.folders.items()}
        found = {}
        for n, (fid, files) in enumerate(self.contents.items(), 1):
            if files:
                found[by_path[fid]] = self.list_folder(fid)
            if on_folder:
                on_folder(n)
        return found

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
    """The real Data Panel. Only works inside Fusion.

    Every request to Fusion is slow, so this asks as little as it can:

    - A folder's subfolders are read once and remembered.
    - `folder.dataFolders` and `folder.dataFiles` are fetched once per
      listing, not once per item.
    - A folder Detent has just made is known to be empty, so it's never
      listed. Listing a brand-new folder is also what Fusion refused on
      5 Oct 2026 ("InternalValidationError : status.isOk() && folders").
    """

    # Fusion's data calls sometimes fail for a moment. Wait this long between
    # tries, in seconds.
    RETRY_WAITS = (0.5, 1.0, 2.0, 4.0)

    def __init__(self, project, root_folder_path: Sequence[str] = ()):
        import adsk.core  # noqa: F401  - fails outside Fusion
        self._project = project
        self._root_path = tuple(root_folder_path)
        self._forget()

    def _forget(self) -> None:
        """Drop every folder object and listing kept so far."""
        # Paths here start at the project, not at the library folder.
        self._folders: Dict[Tuple[str, ...], object] = {}
        self._children: Dict[Tuple[str, ...], Dict[str, object]] = {}

    def wait(self, seconds: float) -> None:
        """Wait without freezing Fusion. Its uploads need the event loop."""
        end = time.time() + seconds
        while True:
            try:
                import adsk
                adsk.doEvents()
            except Exception:                         # noqa: BLE001
                pass
            left = end - time.time()
            if left <= 0:
                return
            time.sleep(min(0.05, left))

    def _retry(self, call):
        """Run a Fusion call. If Fusion reports an error, wait and try again."""
        waits = list(self.RETRY_WAITS)
        while True:
            try:
                return call()
            except Exception:                         # noqa: BLE001
                if not waits:
                    raise
                self.wait(waits.pop(0))

    def _folder(self, path: Tuple[str, ...]):
        if not path and path not in self._folders:
            self._folders[path] = self._retry(lambda: self._project.rootFolder)
        return self._folders[path]

    def _subfolders(self, path: Tuple[str, ...]) -> Dict[str, object]:
        """Name -> folder for one folder's subfolders. Asked once per folder."""
        if path not in self._children:
            folder = self._folder(path)

            def read():
                found: Dict[str, object] = {}
                listing = folder.dataFolders          # one request; reuse it
                for i in range(listing.count):
                    sub = listing.item(i)
                    found.setdefault(sub.name, sub)
                return found

            self._children[path] = self._retry(read)
        return self._children[path]

    def _make(self, parent: Tuple[str, ...], name: str):
        """Create a subfolder. If Fusion errors, check whether it was made
        anyway before trying again, so a retry can't make it twice."""
        waits = list(self.RETRY_WAITS)
        while True:
            try:
                return self._folder(parent).dataFolders.add(name)
            except Exception:                         # noqa: BLE001
                if not waits:
                    raise
                self.wait(waits.pop(0))
                self._children.pop(parent, None)
                found = self._subfolders(parent).get(name)
                if found is not None:
                    return found

    def _walk(self, path: Tuple[str, ...], create: bool):
        """The folder at `path` from the project. None if it doesn't exist and
        create is False."""
        known = len(path)
        while known and path[:known] not in self._folders:
            known -= 1
        here = path[:known]
        self._folder(here)
        for name in path[known:]:
            children = self._subfolders(here)
            folder = children.get(name)
            if folder is None:
                if not create:
                    return None
                folder = self._make(here, name)
                children = self._subfolders(here)
                children[name] = folder
                self._children[here + (name,)] = {}   # just made, so empty
            here = here + (name,)
            self._folders[here] = folder
        return self._folders[here]

    def _open(self, folders: Sequence[str], create: bool):
        path = self._root_path + tuple(folders)
        try:
            return self._walk(path, create)
        except Exception:                             # noqa: BLE001
            # The folder objects kept so far may have gone stale. Start again
            # from the project.
            self._forget()
            try:
                return self._walk(path, create)
            except Exception as exc:                  # noqa: BLE001
                where = "/".join(path) or "the project"
                raise PanelError(
                    f"Fusion wouldn't open {where!r}: {exc}") from exc

    def ensure_folder(self, folders: Sequence[str]):
        """The folder, created if it doesn't exist."""
        return self._open(folders, create=True)

    def peek_folder(self, folders: Sequence[str]):
        """The folder, or None if it doesn't exist. Creates nothing."""
        return self._open(folders, create=False)

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
            listing = folder.dataFiles                # one request; reuse it
            for i in range(listing.count):
                f = listing.item(i)
                out.append(PlacedFile(name=f.name, lineage=f.id,
                                      version=getattr(f, "versionNumber", 1) or 1))
            return out
        return self._retry(read)

    def find_by_name(self, folder, name: str) -> List[PlacedFile]:
        return [f for f in self.list_folder(folder) if f.name == name]

    def scan(self, on_folder=None) -> Dict[Tuple[str, ...], List[PlacedFile]]:
        """Every file in every folder of the library. Slow: one request per
        folder. `on_folder(count)` is called as each folder is read."""
        found: Dict[Tuple[str, ...], List[PlacedFile]] = {}
        if self.peek_folder(()) is None:
            return found
        seen = [0]

        def walk(rel: Tuple[str, ...]):
            path = self._root_path + rel
            files = self.list_folder(self._folder(path))
            if files:
                found[rel] = files
            seen[0] += 1
            if on_folder:
                on_folder(seen[0])
            for name, sub in sorted(self._subfolders(path).items()):
                self._folders[path + (name,)] = sub
                walk(rel + (name,))

        walk(())
        return found
