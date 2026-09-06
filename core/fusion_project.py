"""Resolving a Fusion project, defensively.

`app.data.activeProject` raises InternalValidationError when the Data Panel
has not been initialised in the session - the data model is lazy and a script
touching it first hits an empty id. Observed on 6 Sep 2026; waking the panel
alone did not fix it, and the activeDocument fallback is what worked.

Kept separate from datapanel.py so the failure modes stay documented and
testable in isolation.
"""

from typing import List, Optional, Tuple
import time


def wake_data_panel(app, log: Optional[List[str]] = None) -> None:
    """Force the lazy data model to initialise before touching it."""
    try:
        app.data.isDataPanelVisible = True
        for _ in range(16):
            _do_events()
            time.sleep(0.25)
        if log is not None:
            log.append("data panel woken")
    except Exception as exc:                       # noqa: BLE001
        if log is not None:
            log.append(f"isDataPanelVisible failed: {exc}")


def _do_events():
    import adsk.core  # noqa: F401
    import adsk
    adsk.doEvents()


def list_projects(app) -> List[Tuple[str, str, object]]:
    """[(hub name, project name, project)] across every hub."""
    out = []
    try:
        hubs = app.data.dataHubs
        for i in range(hubs.count):
            hub = hubs.item(i)
            try:
                projects = hub.dataProjects
                for j in range(projects.count):
                    p = projects.item(j)
                    out.append((hub.name, p.name, p))
            except Exception:                      # noqa: BLE001
                continue
    except Exception:                              # noqa: BLE001
        pass
    return out


def resolve_project(app, project_id: Optional[str] = None,
                    log: Optional[List[str]] = None):
    """Find a usable project, trying every route before giving up.

    Order matters: the stored id is authoritative, activeProject is the
    documented route, and activeDocument is the one that actually worked when
    activeProject raised.
    """
    if log is None:
        log = []
    wake_data_panel(app, log)

    if project_id:
        for _hub, _name, proj in list_projects(app):
            try:
                if proj.id == project_id:
                    log.append(f"project by stored id: {proj.name}")
                    return proj
            except Exception:                      # noqa: BLE001
                continue
        log.append(f"stored project id not found: {project_id}")

    try:
        p = app.data.activeProject
        if p:
            log.append(f"project via activeProject: {p.name}")
            return p
    except Exception as exc:                       # noqa: BLE001
        log.append(f"activeProject raised: {exc}")

    try:
        df = app.activeDocument.dataFile
        if df and df.parentProject:
            log.append(f"project via activeDocument: {df.parentProject.name}")
            return df.parentProject
    except Exception as exc:                       # noqa: BLE001
        log.append(f"activeDocument.parentProject raised: {exc}")

    found = list_projects(app)
    if found:
        log.append(f"project via enumeration: {found[0][1]}")
        return found[0][2]

    log.append("no project could be resolved")
    return None
