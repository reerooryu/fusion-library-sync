"""GitHub source: resolve a ref, read its tree, fetch blobs.

Transport is injected so every path here is testable offline. Bootstrap pulls
one tarball; deltas fetch individual files.
"""

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Protocol, Sequence, Tuple
import io
import json
import os
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com"
CODELOAD = "https://codeload.github.com"

# Above this many files, one tarball beats N requests. Tune with real timings.
TARBALL_THRESHOLD = 100

# GitHub builds a codeload archive on demand; for a 2.2 GB repository that
# routinely times out at the edge. These are the statuses worth trying again -
# a 404 or a 403 will never change.
RETRY_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
RETRIES = 4
BACKOFF = 2.0          # seconds, doubling


class Transport(Protocol):
    def get_json(self, url: str) -> Tuple[dict, Dict[str, str]]: ...
    def get_bytes(self, url: str, on_chunk=None) -> bytes: ...


class UrllibTransport:
    """Default transport. No third-party dependencies - Fusion ships plain CPython."""

    def __init__(self, token: Optional[str] = None, timeout: int = 60,
                 retries: int = RETRIES, sleep=time.sleep):
        self.token = token
        self.timeout = timeout
        self.retries = retries
        self._sleep = sleep          # injected so tests do not actually wait
        self.attempts = 0            # observable: how many requests were made

    def _req(self, url: str) -> urllib.request.Request:
        headers = {"User-Agent": "detent", "Accept": "application/vnd.github+json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return urllib.request.Request(url, headers=headers)

    def _open(self, url):
        """urlopen, retried on the failures that are worth retrying.

        One 504 from codeload used to end a whole sync. Reported live on a
        first full-library run, which is exactly when the archive is biggest
        and the edge most likely to give up.
        """
        last = None
        for attempt in range(self.retries + 1):
            self.attempts += 1
            try:
                return urllib.request.urlopen(self._req(url), timeout=self.timeout)
            except urllib.error.HTTPError as exc:
                if exc.code not in RETRY_STATUS:
                    raise
                last = exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                # Connection reset, DNS blip, read timeout. Same treatment.
                last = exc
            if attempt < self.retries:
                self._sleep(BACKOFF * (2 ** attempt))
        raise GitHubError(
            f"{url}\ngave up after {self.retries + 1} attempts: {last}")

    def get_json(self, url):
        with self._open(url) as r:
            return json.loads(r.read().decode("utf-8")), dict(r.headers)

    def get_bytes(self, url, on_chunk=None):
        """on_chunk(received, total) is called as data arrives, so a caller on
        a UI thread can pump events and stay cancellable."""
        with self._open(url) as r:
            if on_chunk is None:
                return r.read()
            total = int(r.headers.get("Content-Length") or 0)
            buf, got = bytearray(), 0
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                buf += chunk
                got += len(chunk)
                if on_chunk(got, total) is False:
                    raise Cancelled("download cancelled")
            return bytes(buf)


class GitHubError(RuntimeError):
    pass


class Cancelled(GitHubError):
    pass


class TreeTruncated(GitHubError):
    """GitHub truncates large trees silently. Read as complete, a truncated
    tree reports every unlisted file as an orphan."""


@dataclass
class Source:
    repo: str                 # "owner/name"
    ref: str = "main"
    subpath: str = ""


def parse_tree(payload: dict) -> Dict[str, str]:
    """{path: blob_sha} for blobs only. Raises if GitHub truncated the response."""
    if payload.get("truncated"):
        raise TreeTruncated(
            "tree truncated by GitHub; repo too large for a single recursive read"
        )
    out: Dict[str, str] = {}
    for entry in payload.get("tree", []):
        if entry.get("type") == "blob":
            out[entry["path"]] = entry["sha"]
    return out


def resolve_commit(src: Source, transport: Transport) -> str:
    """Pin the ref to a commit SHA so a sync is reproducible mid-run."""
    url = f"{API}/repos/{src.repo}/commits/{urllib.parse.quote(src.ref)}"
    payload, _ = transport.get_json(url)
    sha = payload.get("sha")
    if not sha:
        raise GitHubError(f"no commit sha for {src.repo}@{src.ref}")
    return sha


def fetch_tree(src: Source, transport: Transport,
               commit: Optional[str] = None) -> Dict[str, str]:
    """One request for every path and blob SHA in the repo."""
    url = (f"{API}/repos/{src.repo}/git/trees/"
           f"{urllib.parse.quote(commit or src.ref)}?recursive=1")
    payload, _ = transport.get_json(url)
    tree = parse_tree(payload)
    if src.subpath:
        prefix = src.subpath.strip("/") + "/"
        tree = {p: s for p, s in tree.items() if p.startswith(prefix)}
    return tree


def raw_url(src: Source, path: str, commit: Optional[str] = None) -> str:
    ref = commit or src.ref
    quoted = "/".join(urllib.parse.quote(seg) for seg in path.split("/"))
    return f"{RAW}/{src.repo}/{ref}/{quoted}"


def fetch_blob(src: Source, path: str, transport: Transport,
               commit: Optional[str] = None) -> bytes:
    """raw.githubusercontent is not counted against the API rate limit."""
    return transport.get_bytes(raw_url(src, path, commit))


def tarball_url(src: Source, commit: Optional[str] = None) -> str:
    return f"{CODELOAD}/{src.repo}/tar.gz/{commit or src.ref}"


def should_use_tarball(n_files: int, threshold: int = TARBALL_THRESHOLD) -> bool:
    return n_files >= threshold


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def human_duration(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f} sec"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min"
    return f"{seconds / 3600:.1f} hours"


# Uploads are fired, not waited on: the earlier 8-18 s figure was the cost of
# blocking on a future that Fusion cannot resolve while blocked, not the cost
# of the upload. This covers download plus fire plus settling.
SECONDS_PER_UPLOAD = 2.0


def estimate(n_files: int, total_bytes: int = 0) -> str:
    parts = [f"{n_files} file(s)"]
    if total_bytes:
        parts.append(human_bytes(total_bytes))
    parts.append(f"about {human_duration(n_files * SECONDS_PER_UPLOAD)}")
    return ", ".join(parts)


def _safe_members(tar: tarfile.TarFile, dest: str):
    """Reject traversal, absolute paths, links. Never trust an archive."""
    dest_abs = os.path.abspath(dest)
    for m in tar.getmembers():
        if m.issym() or m.islnk():
            continue
        if not m.isfile():
            continue
        target = os.path.abspath(os.path.join(dest, m.name))
        if not target.startswith(dest_abs + os.sep):
            raise GitHubError(f"unsafe path in archive: {m.name!r}")
        yield m


def extract_tarball(data: bytes, dest: str,
                    wanted: Optional[Sequence[str]] = None) -> Dict[str, str]:
    """Unpack a codeload tarball, stripping its top-level dir.
    Returns {repo_path: local_path}."""
    want = set(wanted) if wanted is not None else None
    written: Dict[str, str] = {}
    os.makedirs(dest, exist_ok=True)

    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for m in _safe_members(tar, dest):
            # codeload wraps everything in "<name>-<ref>/"
            _, _, rel = m.name.partition("/")
            if not rel:
                continue
            if want is not None and rel not in want:
                continue
            fh = tar.extractfile(m)
            if fh is None:
                continue
            out = os.path.join(dest, rel.replace("/", os.sep))
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(out, "wb") as w:
                w.write(fh.read())
            written[rel] = out
    return written


def fetch_files(src: Source, paths: Sequence[str], dest: str,
                transport: Transport, commit: Optional[str] = None,
                on_progress: Optional[Callable[[int, int, str], None]] = None,
                threshold: int = TARBALL_THRESHOLD) -> Tuple[Dict[str, str], List[Tuple[str, str]]]:
    """Returns (fetched, failures). One unreachable file never aborts a run."""
    paths = list(paths)
    fetched: Dict[str, str] = {}
    failures: List[Tuple[str, str]] = []

    if should_use_tarball(len(paths), threshold):
        def chunk(got, total):
            if on_progress:
                pct = f"{got * 100 // total}%" if total else human_bytes(got)
                return on_progress(0, len(paths), f"downloading {pct}")
            return None
        try:
            data = transport.get_bytes(tarball_url(src, commit), on_chunk=chunk)
        except Cancelled:
            raise
        except Exception as exc:                     # noqa: BLE001
            # GitHub builds the archive on demand and the big ones time out at
            # the edge; one 504 used to end the run. Individual files come from
            # a different host, are small, and are not rate limited - much
            # slower, but it finishes.
            if on_progress:
                on_progress(0, len(paths),
                            f"archive failed ({type(exc).__name__}), "
                            "fetching files individually")
        else:
            fetched = extract_tarball(data, dest, wanted=paths)
            for p in paths:
                if p not in fetched:
                    failures.append((p, "not present in tarball"))
            if on_progress:
                on_progress(len(fetched), len(paths), "tarball")
            return fetched, failures

    for i, p in enumerate(paths, 1):
        try:
            blob = fetch_blob(src, p, transport, commit)
            out = os.path.join(dest, p.replace("/", os.sep))
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(out, "wb") as w:
                w.write(blob)
            fetched[p] = out
        except Exception as exc:                     # noqa: BLE001 - report, continue
            failures.append((p, f"{type(exc).__name__}: {exc}"))
        if on_progress:
            on_progress(i, len(paths), p)

    return fetched, failures
