"""Config: which libraries to sync, and where they go.

JSON beside the add-in, hand-edited in v0.1. Ships with VEX-CAD preconfigured
so the common case never involves pasting a repo URL.
"""

from dataclasses import dataclass, field, asdict
from typing import List
import json
import os
import re
import tempfile

SCHEMA = 1

_REPO_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
_URL_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?github\.com/([A-Za-z0-9._-]+/[A-Za-z0-9._-]+?)(?:\.git)?/?$"
)


class ConfigError(ValueError):
    pass


def normalise_repo(value: str) -> str:
    """Accept 'owner/name' or any github.com URL people paste."""
    v = (value or "").strip()
    if not v:
        raise ConfigError("empty repository")
    m = _URL_RE.match(v)
    if m:
        return m.group(1)
    if _REPO_RE.match(v):
        return v
    raise ConfigError(f"not a GitHub repo: {value!r}")


@dataclass
class SourceConfig:
    id: str
    label: str
    repo: str
    ref: str = "main"
    subpath: str = ""
    include: List[str] = field(default_factory=lambda: ["**/*.f3d"])
    exclude: List[str] = field(default_factory=list)
    folder_path: str = ""                 # below the project root
    # Walk the Data Panel each run to check the manifest is still true. Costs
    # one listing per folder; without it, files deleted by hand stay invisible.
    verify_placed: bool = True

    def __post_init__(self):
        self.repo = normalise_repo(self.repo)
        if not self.id:
            raise ConfigError("source needs an id")
        if self.subpath.strip("/"):
            # paths.map_path takes a subpath, and adopt_existing passes it -
            # but apply_plan, settle, reconcile_inflight and detect_drift do
            # not. Setting one makes sync mirror the prefix into the Data
            # Panel while adopt looks for it stripped, so adopt matches
            # nothing, writes an empty manifest, and the next sync uploads a
            # duplicate of the entire library. Refuse until it is threaded
            # through everywhere.
            raise ConfigError(
                f"subpath is not supported yet (source {self.id!r}); "
                "narrow with include patterns instead, e.g. "
                '"include": ["cad/**/*.f3d"]')

    @property
    def folders(self) -> List[str]:
        return [p for p in self.folder_path.split("/") if p]

    @property
    def manifest_name(self) -> str:
        return f"{self.id}.manifest.json"


@dataclass
class Config:
    schema: int = SCHEMA
    sources: List[SourceConfig] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"schema": self.schema, "sources": [asdict(s) for s in self.sources]}

    @classmethod
    def from_dict(cls, d: dict) -> "Config":
        got = d.get("schema", 0)
        if got != SCHEMA:
            raise ConfigError(f"config schema {got}, expected {SCHEMA}")
        srcs = []
        for raw in d.get("sources", []):
            known = {k: v for k, v in raw.items()
                     if k in SourceConfig.__dataclass_fields__}
            srcs.append(SourceConfig(**known))
        ids = [s.id for s in srcs]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            raise ConfigError(f"duplicate source ids: {sorted(dupes)}")
        return cls(schema=got, sources=srcs)

    def save(self, path: str) -> None:
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
    def load(cls, path: str) -> "Config":
        if not os.path.exists(path):
            return default_config()
        with open(path, encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh))

    @classmethod
    def load_or_create(cls, path: str) -> "Config":
        """First launch writes the defaults so the file is there to edit."""
        if os.path.exists(path):
            return cls.load(path)
        cfg = default_config()
        cfg.save(path)
        return cfg


def default_config() -> Config:
    return Config(sources=[
        SourceConfig(
            id="vex-cad",
            label="VEX CAD Library",
            repo="VEX-CAD/VEX-CAD-Fusion-360-Library",
            ref="main",
            include=["**/*.f3d"],
            folder_path="VEX CAD Library",
        )
    ])
