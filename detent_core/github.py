"""GitHub: resolve a ref, read the file tree, download files.

The transport is passed in so tests run without a network. Files are
downloaded individually, several at once. The whole-repo archive is only a
fallback.
"""

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Protocol, Sequence, Tuple
import concurrent.futures
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

# Files downloaded at once. Measured on a home connection (23 Sep 2026):
# 16 at once ran at 117 Mbit/s; the single archive stream at 10 Mbit/s.
DOWNLOAD_WORKERS = 16

# If at least this many downloads fail (for example GitHub rate-limiting),
# get the whole-repo archive and take the failed files from it.
ARCHIVE_FALLBACK_MIN = 100

# Statuses worth retrying. A 404 or 403 won't change.
RETRY_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
RETRIES = 4
BACKOFF = 2.0          # seconds, doubling


class Transport(Protocol):
    def get_json(self, url: str) -> Tuple[dict, Dict[str, str]]: ...
    def get_bytes(self, url: str, on_chunk=None) -> bytes: ...


class UrllibTransport:
    """Default transport. Standard library only, since Fusion ships plain Python."""

    def __init__(self, token: Optional[str] = None, timeout: int = 60,
                 retries: int = RETRIES, sleep=time.sleep):
        self.token = token
        self.timeout = timeout
        self.retries = retries
        self._sleep = sleep          # injectable so tests don't wait
        self.attempts = 0            # request count, for tests

    def _req(self, url: str) -> urllib.request.Request:
        headers = {"User-Agent": "detent", "Accept": "application/vnd.github+json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return urllib.request.Request(url, headers=headers)

    def _open(self, url):
        """urlopen, retrying timeouts, resets and server errors."""
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
                last = exc
            if attempt < self.retries:
                self._sleep(BACKOFF * (2 ** attempt))
        raise GitHubError(
            f"{url}\ngave up after {self.retries + 1} attempts: {last}")

    def get_json(self, url):
        with self._open(url) as r:
            return json.loads(r.read().decode("utf-8")), dict(r.headers)

    def get_bytes(self, url, on_chunk=None):
        """on_chunk(received, total) is called as data arrives. Returning False
        cancels."""
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
    """GitHub cut the tree short. Treating it as complete would make every
    missing file look deleted."""


@dataclass
class Source:
    repo: str                 # "owner/name"
    ref: str = "main"
    subpath: str = ""


class Tree(dict):
    """{path: blob_sha}, plus a size in bytes for each file in `.sizes`."""

    def __init__(self, *args, sizes=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.sizes: Dict[str, int] = sizes or {}

    def bytes_for(self, paths) -> int:
        return sum(self.sizes.get(p, 0) for p in paths)

    @property
    def total_bytes(self) -> int:
        return sum(self.sizes.values())


def parse_tree(payload: dict) -> Tree:
    """Files only, not folders. Raises if GitHub truncated the response."""
    if payload.get("truncated"):
        raise TreeTruncated(
            "tree truncated by GitHub; repo too large for a single recursive read"
        )
    out: Dict[str, str] = {}
    sizes: Dict[str, int] = {}
    for entry in payload.get("tree", []):
        if entry.get("type") == "blob":
            out[entry["path"]] = entry["sha"]
            size = entry.get("size")
            if isinstance(size, int):
                sizes[entry["path"]] = size
    return Tree(out, sizes=sizes)


def resolve_commit(src: Source, transport: Transport) -> str:
    """The commit SHA for the ref, so the whole sync uses one version."""
    url = f"{API}/repos/{src.repo}/commits/{urllib.parse.quote(src.ref)}"
    payload, _ = transport.get_json(url)
    sha = payload.get("sha")
    if not sha:
        raise GitHubError(f"no commit sha for {src.repo}@{src.ref}")
    return sha


def list_releases(src: Source, transport: Transport,
                  limit: int = 20) -> List[str]:
    """Release tags, newest first, without drafts. Falls back to plain tags
    for a repo that tags without making releases. Empty if it has neither."""
    n = max(1, min(limit, 100))
    payload, _ = transport.get_json(
        f"{API}/repos/{src.repo}/releases?per_page={n}")
    tags = [r["tag_name"] for r in payload if isinstance(r, dict)
            and r.get("tag_name") and not r.get("draft")] \
        if isinstance(payload, list) else []
    if not tags:
        payload, _ = transport.get_json(
            f"{API}/repos/{src.repo}/tags?per_page={n}")
        tags = [t["name"] for t in payload if isinstance(t, dict)
                and t.get("name")] if isinstance(payload, list) else []
    return tags[:limit]


def fetch_tree(src: Source, transport: Transport,
               commit: Optional[str] = None) -> Tree:
    """Every file path and blob SHA in the repo, in one request."""
    url = (f"{API}/repos/{src.repo}/git/trees/"
           f"{urllib.parse.quote(commit or src.ref)}?recursive=1")
    payload, _ = transport.get_json(url)
    tree = parse_tree(payload)
    if src.subpath:
        prefix = src.subpath.strip("/") + "/"
        kept = {p: s for p, s in tree.items() if p.startswith(prefix)}
        # Keep it a Tree so the sizes survive.
        tree = Tree(kept, sizes={p: n for p, n in tree.sizes.items() if p in kept})
    return tree


def raw_url(src: Source, path: str, commit: Optional[str] = None) -> str:
    ref = commit or src.ref
    quoted = "/".join(urllib.parse.quote(seg) for seg in path.split("/"))
    return f"{RAW}/{src.repo}/{ref}/{quoted}"


def fetch_blob(src: Source, path: str, transport: Transport,
               commit: Optional[str] = None) -> bytes:
    """raw.githubusercontent.com doesn't count against the API rate limit."""
    return transport.get_bytes(raw_url(src, path, commit))


def tarball_url(src: Source, commit: Optional[str] = None) -> str:
    return f"{CODELOAD}/{src.repo}/tar.gz/{commit or src.ref}"


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


# What a sync costs, measured on one Mac on 6-7 Oct 2026:
#   making 115 folders took 93 s; handing 1,165 files to Fusion took 162 s;
#   1.9 GB downloaded in 65 s and 250 MB in 11 s (about 25 MB/s).
# The download rate below is half the measured one, since connections vary.
# This is the time until Detent is done. Fusion then uploads in the
# background, and that isn't counted.
SECONDS_TO_START = 5.0
SECONDS_PER_FOLDER = 0.8
SECONDS_PER_FILE = 0.15
DOWNLOAD_BYTES_PER_SECOND = 12_000_000


def estimate_seconds(n_files: int, total_bytes: int = 0, folders: int = 0) -> float:
    return (SECONDS_TO_START + folders * SECONDS_PER_FOLDER
            + n_files * SECONDS_PER_FILE
            + total_bytes / DOWNLOAD_BYTES_PER_SECOND)


def estimate(n_files: int, total_bytes: int = 0, folders: int = 0) -> str:
    parts = [f"{n_files} file(s)"]
    if total_bytes:
        parts.append(human_bytes(total_bytes))
    seconds = estimate_seconds(n_files, total_bytes, folders)
    parts.append(f"about {human_duration(seconds)}")
    return ", ".join(parts)


def _safe_members(tar: tarfile.TarFile, dest: str):
    """Skip links, and refuse any path that would land outside dest."""
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
    """Unpack a GitHub archive without its top-level folder.
    Returns {repo_path: local_path}."""
    want = set(wanted) if wanted is not None else None
    written: Dict[str, str] = {}
    os.makedirs(dest, exist_ok=True)

    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for m in _safe_members(tar, dest):
            # GitHub wraps everything in "<name>-<ref>/".
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
                workers: int = 0,
                fallback_at: int = ARCHIVE_FALLBACK_MIN
                ) -> Tuple[Dict[str, str], List[Tuple[str, str]]]:
    """Download files, several at once. Returns (fetched, failures).

    One failed file doesn't stop the rest. If `fallback_at` or more fail, the
    failed ones are taken from the whole-repo archive instead. 0 turns the
    fallback off.
    """
    paths = list(paths)
    fetched, failures = _fetch_each(src, paths, dest, transport, commit,
                                    on_progress, workers)
    if fallback_at and len(failures) >= fallback_at:
        got = _fetch_from_archive(src, [p for p, _m in failures], dest,
                                  transport, commit, on_progress)
        fetched.update(got)
        failures = [(p, m) for p, m in failures if p not in got]
    return fetched, failures


def _fetch_each(src, paths, dest, transport, commit, on_progress, workers):
    fetched: Dict[str, str] = {}
    failures: List[Tuple[str, str]] = []

    def one(path):
        blob = fetch_blob(src, path, transport, commit)
        out = os.path.join(dest, path.replace("/", os.sep))
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "wb") as w:
            w.write(blob)
        return out

    if not paths:
        return fetched, failures

    # Downloads run in threads. Progress and cancel stay on this thread, which
    # is the only one allowed to touch Fusion.
    workers = max(1, min(workers or DOWNLOAD_WORKERS, len(paths)))
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        pending = {pool.submit(one, p): p for p in paths}
        try:
            for fut in concurrent.futures.as_completed(pending):
                path = pending[fut]
                try:
                    fetched[path] = fut.result()
                except Exception as exc:             # noqa: BLE001 - report, continue
                    failures.append((path, f"{type(exc).__name__}: {exc}"))
                done += 1
                if on_progress and on_progress(
                        done, len(paths),
                        f"Downloading {done}/{len(paths)}") is False:
                    raise Cancelled("download cancelled")
        except BaseException:
            # Cancel anything not started yet.
            for f in pending:
                f.cancel()
            raise
    return fetched, failures


def _fetch_from_archive(src, paths, dest, transport, commit,
                        on_progress) -> Dict[str, str]:
    """Take `paths` from the whole-repo archive. Returns what it found; if the
    archive itself fails, returns nothing and the original failures stand."""
    n = len(paths)
    if on_progress:
        on_progress(0, n, f"{n} downloads failed, getting the full archive")

    def chunk(got, total):
        # Progress by bytes, scaled to the file count.
        if not on_progress:
            return None
        # No "%": Detent.py escapes it for Fusion's progress dialog.
        if total:
            return on_progress(n * got // total, n,
                               f"Downloading archive {human_bytes(got)} "
                               f"of {human_bytes(total)}")
        return on_progress(0, n, f"Downloading archive {human_bytes(got)}")

    try:
        data = transport.get_bytes(tarball_url(src, commit), on_chunk=chunk)
    except Cancelled:
        raise
    except Exception:                                # noqa: BLE001
        return {}
    return extract_tarball(data, dest, wanted=paths)
