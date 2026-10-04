"""A short ID for the exact code in an add-in folder.

Two builds can share a version number. The build ID changes whenever any
code file changes, so it shows which build Fusion is really running.
"""

import hashlib
import os


def build_id(root: str) -> str:
    """7-character ID from Detent.py and every .py file in detent_core/."""
    names = ["Detent.py"]
    core = os.path.join(root, "detent_core")
    try:
        names += sorted("detent_core/" + n for n in os.listdir(core)
                        if n.endswith(".py"))
    except OSError:
        pass

    digest = hashlib.sha1()
    for name in names:
        try:
            with open(os.path.join(root, *name.split("/")), "rb") as fh:
                data = fh.read()
        except OSError:
            continue
        # Windows checkouts may use CRLF. Same code, same ID.
        digest.update(name.encode() + b"\0" + data.replace(b"\r\n", b"\n") + b"\0")
    return digest.hexdigest()[:7]


def version_line(version: str, core_version: str, loaded: str, on_disk: str) -> str:
    """The note at the bottom of the dialog.

    loaded is the build Fusion imported; on_disk is the build in the add-in
    folder now. They differ after an update, until Fusion restarts.
    """
    if core_version != version:
        return (f"Detent {version} but core {core_version} - STALE MODULE. "
                "Restart Fusion.")
    if on_disk != loaded:
        return (f"Detent {version} (build {loaded}) - OUT OF DATE. The files "
                f"were updated to build {on_disk}. Restart Fusion.")
    return f"Detent {version} (build {loaded})"
