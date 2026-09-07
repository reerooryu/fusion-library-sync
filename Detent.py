"""Add-in entry point: Utilities > ADD-INS > Sync Library.

Presentation only. The risky logic lives in core/ and is tested without Fusion.
"""

import os
import sys
import traceback

import adsk.core
import adsk.fusion

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from core import config as C            # noqa: E402
from core import github as gh           # noqa: E402
from core import sync as S              # noqa: E402
from core import fusion_project as FP   # noqa: E402
from core.datapanel import FusionDataPanel  # noqa: E402

CMD_ID = "DetentSyncLibrary"
CMD_NAME = "Sync Library"
CMD_TIP = ("Sync a Git-hosted CAD library into this project.\n\n"
           "Downloads only what changed. Never modifies a file it has "
           "already placed.")
PANEL_ID = "SolidScriptsAddinsPanel"    # Utilities tab > ADD-INS

CONFIG_PATH = os.path.join(_HERE, "config.json")
STATE_DIR = os.path.join(_HERE, "state")
LOG_PATH = os.path.join(STATE_DIR, "detent.log")

_handlers = []      # Fusion garbage-collects handlers that aren't referenced


def _log(text):
    """Fusion message boxes truncate, and they ate the one line that mattered:
    the exception type. Always write the full text somewhere readable."""
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(f"\n===== {__import__('datetime').datetime.now()} =====\n")
            fh.write(text)
    except Exception:
        pass
    return LOG_PATH


def _fail(ui, what):
    detail = traceback.format_exc()
    path = _log(f"{what}\n{detail}")
    ui.messageBox(f"{what}\n\n{detail[-700:]}\n\nFull log:\n{path}")


def _app():
    return adsk.core.Application.get()


def _ui():
    return _app().userInterface


# --------------------------------------------------------------------------
class CommandCreatedHandler(adsk.core.CommandCreatedEventHandler):
    def __init__(self):
        super().__init__()

    def notify(self, args):
        try:
            cmd = args.command
            cmd.isExecutedWhenPreEmpted = False
            inputs = cmd.commandInputs

            cfg = C.Config.load_or_create(CONFIG_PATH)

            src_in = inputs.addDropDownCommandInput(
                "source", "Library", adsk.core.DropDownStyles.TextListDropDownStyle)
            if cfg.sources:
                for s in cfg.sources:
                    src_in.listItems.add(s.label, s is cfg.sources[0], "")
            else:
                src_in.listItems.add("(none configured)", True, "")

            log = []
            projects = FP.list_projects(_app())
            proj_in = inputs.addDropDownCommandInput(
                "project", "Project", adsk.core.DropDownStyles.TextListDropDownStyle)
            if projects:
                active = FP.resolve_project(_app(), None, log)
                active_name = getattr(active, "name", None)
                for _hub, name, _p in projects:
                    proj_in.listItems.add(name, name == active_name, "")
                if not any(i.isSelected for i in proj_in.listItems):
                    proj_in.listItems.item(0).isSelected = True
            else:
                proj_in.listItems.add("(no projects found)", True, "")

            folder_in = inputs.addStringValueInput(
                "folder", "Folder", cfg.sources[0].folder_path if cfg.sources else "")
            folder_in.tooltip = "Folder inside the project. Created if absent."

            action = inputs.addDropDownCommandInput(
                "action", "Action", adsk.core.DropDownStyles.TextListDropDownStyle)
            action.listItems.add("Preview changes", True, "")
            action.listItems.add("Sync now", False, "")
            action.listItems.add("Adopt existing library", False, "")
            action.tooltip = (
                "Preview lists what would change and writes nothing.\n"
                "Sync uploads new files only.\n"
                "Adopt claims a library you already imported, without uploading.")

            adopt_ref = inputs.addStringValueInput(
                "adopt_ref", "Installed release", "")
            adopt_ref.tooltip = (
                "For Adopt: the release tag your existing library came from, "
                "e.g. v2.0.3. Leave blank if you do not know - files are then "
                "marked unverified rather than assumed current.")

            note = inputs.addTextBoxCommandInput(
                "note", "", _config_note(cfg.sources[0] if cfg.sources else None),
                3, True)
            note.isFullWidth = True

            on_exec = ExecuteHandler()
            cmd.execute.add(on_exec)
            _handlers.append(on_exec)

            on_changed = InputChangedHandler(cfg)
            cmd.inputChanged.add(on_changed)
            _handlers.append(on_changed)
        except Exception:
            _fail(_ui(), "Detent failed to open")


class InputChangedHandler(adsk.core.InputChangedEventHandler):
    """Folder and note must follow the Library dropdown. Without this, picking
    a second library and pressing OK writes the first library's folder into
    it - and the two libraries land on top of each other."""

    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg

    def notify(self, args):
        try:
            if args.input.id != "source":
                return
            inputs = args.inputs
            label = inputs.itemById("source").selectedItem.name
            source = next((s for s in self.cfg.sources if s.label == label), None)
            if source is None:
                return
            inputs.itemById("folder").value = source.folder_path
            inputs.itemById("note").text = _config_note(source)
        except Exception:
            _fail(_ui(), "Detent failed to switch library")


def _config_note(source) -> str:
    if source is None:
        return f"No sources configured. Edit:\n{CONFIG_PATH}"
    return (f"{source.repo} @ {source.ref}\n"
            f"Matching: {', '.join(source.include)}\n"
            f"Edit sources in config.json beside the add-in.")


# --------------------------------------------------------------------------
class ExecuteHandler(adsk.core.CommandEventHandler):
    def __init__(self):
        super().__init__()

    def notify(self, args):
        ui = _ui()
        try:
            inputs = args.command.commandInputs
            cfg = C.Config.load_or_create(CONFIG_PATH)
            if not cfg.sources:
                ui.messageBox(f"No sources configured.\n\n{CONFIG_PATH}")
                return

            label = inputs.itemById("source").selectedItem.name
            source = next((s for s in cfg.sources if s.label == label), cfg.sources[0])

            proj_name = inputs.itemById("project").selectedItem.name
            folder_path = inputs.itemById("folder").value.strip()
            action = inputs.itemById("action").selectedItem.name
            adopt_ref = inputs.itemById("adopt_ref").value.strip() or None

            log = []
            project = _pick_project(proj_name, log)
            if project is None:
                ui.messageBox("Could not resolve a Fusion project.\n\n" + "\n".join(log))
                return

            source.folder_path = folder_path
            cfg.save(CONFIG_PATH)

            os.makedirs(STATE_DIR, exist_ok=True)
            manifest_path = os.path.join(STATE_DIR, source.manifest_name)
            panel = FusionDataPanel(project, source.folders)
            src = gh.Source(source.repo, source.ref, source.subpath)
            transport = gh.UrllibTransport()

            if action.startswith("Adopt"):
                _do_adopt(ui, src, manifest_path, panel, transport, source, adopt_ref)
            else:
                _do_sync(ui, src, manifest_path, panel, transport, source,
                         write=action.startswith("Sync"))
        except Exception:
            _fail(ui, "Detent failed")


def _pick_project(name, log):
    for _hub, pname, proj in FP.list_projects(_app()):
        if pname == name:
            return proj
    return FP.resolve_project(_app(), None, log)


def _do_sync(ui, src, manifest_path, panel, transport, source, write: bool):
    """Always plan first. Uploading requires a second, explicit yes; a sync
    that finds nothing still runs, so the manifest records the check."""
    progress = ui.createProgressDialog()
    progress.isCancelButtonShown = False
    progress.show("Detent", "Reading %v...", 0, 1)
    adsk.doEvents()
    try:
        plan, _ = S.sync(src, manifest_path, panel, transport,
                         source.include, source.exclude, dry_run=True)
    finally:
        progress.hide()

    summary = _plan_text(plan, source) if not plan.is_empty else "Everything is up to date."
    if not write:
        ui.messageBox(summary, "Detent - preview")
        return

    # Anything to upload needs an explicit yes. A run with nothing to upload
    # still goes through the write path below: it reconciles interrupted
    # uploads and records that this check happened, which is exactly the case
    # the manifest header used to miss.
    if plan.add:
        n = len(plan.add)
        warn = ""
        if n >= gh.TARBALL_THRESHOLD:
            warn = ("\n\nThis is a full first sync. It downloads the whole "
                    "repository in one go and Fusion will be UNRESPONSIVE for "
                    "most of it.\n\nConsider narrowing 'include' in config.json "
                    "and syncing a subset first.")
        answer = ui.messageBox(
            f"{summary}\n\nUpload {gh.estimate(n)}?{warn}",
            "Detent", adsk.core.MessageBoxButtonTypes.YesNoButtonType)
        if answer != adsk.core.DialogResults.DialogYes:
            return

    progress = ui.createProgressDialog()
    progress.isCancelButtonShown = True
    progress.show("Detent", "Starting...", 0, max(len(plan.add), 1))

    def on_progress(i, total, label):
        # Returning False asks core to abort; it is how cancel reaches a
        # long download or a slow upload.
        if progress.wasCancelled:
            return False
        if label:
            progress.message = label.replace("%", "%%")
        progress.progressValue = min(max(i, 0), total)
        adsk.doEvents()
        return True

    try:
        _plan2, report = S.sync(src, manifest_path, panel, transport,
                                source.include, source.exclude,
                                dry_run=False, on_progress=on_progress)
    except gh.Cancelled:
        progress.hide()
        ui.messageBox("Cancelled. Nothing was left half-written - rerun to "
                      "pick up where it stopped.", "Detent")
        return
    finally:
        progress.hide()

    ui.messageBox(_report_text(report, manifest_path), "Detent - done")


def _do_adopt(ui, src, manifest_path, panel, transport, source, at_ref):
    if os.path.exists(manifest_path):
        answer = ui.messageBox(
            "A manifest already exists for this library. Adopting will replace "
            "it.\n\nContinue?", "Detent",
            adsk.core.MessageBoxButtonTypes.YesNoButtonType)
        if answer != adsk.core.DialogResults.DialogYes:
            return

    progress = ui.createProgressDialog()
    progress.isCancelButtonShown = False
    progress.show("Detent", "Scanning existing library...", 0, 1)
    adsk.doEvents()
    try:
        manifest, stats = S.adopt_existing(
            src, manifest_path, panel, transport, at_ref,
            source.include, dry_run=True)
    finally:
        progress.hide()

    known = at_ref or "unknown"
    text = (f"Adopt {src.repo} at {known}\n\n"
            f"  files in that release   {stats['in_release']}\n"
            f"  files in your project   {stats['in_panel']}\n"
            f"  matched                 {stats['matched']}\n"
            f"  not found locally       {stats['missing']}\n\n"
            "Adopting uploads nothing. It records what you already have so "
            "the next sync moves only the difference.")
    if not at_ref:
        text += ("\n\nNo release given: files are recorded as unverified "
                 "rather than assumed current.")

    answer = ui.messageBox(text + "\n\nWrite the manifest?", "Detent",
                           adsk.core.MessageBoxButtonTypes.YesNoButtonType)
    if answer != adsk.core.DialogResults.DialogYes:
        return

    manifest.save(manifest_path)
    ui.messageBox(f"Adopted {stats['matched']} file(s).\n\n{manifest_path}",
                  "Detent")


def _plan_text(plan, source) -> str:
    lines = [f"{source.repo} @ {source.ref}", ""]
    lines.append(f"  + {len(plan.add):5d}  to add")
    if plan.change:
        lines.append(f"  ~ {len(plan.change):5d}  changed upstream (Phase 2)")
    if plan.orphan:
        lines.append(f"  - {len(plan.orphan):5d}  gone upstream (cannot delete)")
    if plan.unverified:
        lines.append(f"  ? {len(plan.unverified):5d}  unverified")
    if plan.inflight:
        lines.append(f"  ! {len(plan.inflight):5d}  interrupted, will reconcile")
    if plan.add:
        lines += ["", f"Estimated: {gh.estimate(len(plan.add))}"]
        if len(plan.add) >= gh.TARBALL_THRESHOLD:
            lines.append("Full first sync - downloads the entire repository.")
        lines += ["", "First few:"]
        lines += [f"    {_short(p)}" for p in plan.add[:8]]
        if len(plan.add) > 8:
            lines.append(f"    ... and {len(plan.add) - 8} more")
    return "\n".join(lines)


def _short(path: str, width: int = 58) -> str:
    """Fusion message boxes wrap long paths into soup. Keep the end, which is
    the part that identifies the file."""
    if len(path) <= width:
        return path
    return "..." + path[-(width - 3):]


def _report_text(report, manifest_path) -> str:
    lines = report.lines()
    if report.collisions:
        lines += ["", "Blocked - two files would land in one place:"]
        lines += [f"  {k}: {', '.join(v)}" for k, v in list(report.collisions.items())[:5]]
    if report.unmappable:
        lines += ["", "Blocked - unmappable paths:"]
        lines += [f"  {p}: {m}" for p, m in report.unmappable[:5]]
    if report.failures:
        lines += ["", "Failures:"]
        lines += [f"  {p}: {m}" for p, m in report.failures[:8]]
        if len(report.failures) > 8:
            lines.append(f"  ... and {len(report.failures) - 8} more")
    if report.renamed:
        lines += ["", "Renamed by Fusion (recorded):"]
        lines += [f"  {p} -> {n}" for p, n in report.renamed[:5]]
    lines += ["", manifest_path]
    return "\n".join(lines)


# --------------------------------------------------------------------------
def run(context):
    try:
        ui = _ui()
        cmd_def = ui.commandDefinitions.itemById(CMD_ID)
        if cmd_def:
            cmd_def.deleteMe()
        cmd_def = ui.commandDefinitions.addButtonDefinition(CMD_ID, CMD_NAME, CMD_TIP)

        on_created = CommandCreatedHandler()
        cmd_def.commandCreated.add(on_created)
        _handlers.append(on_created)

        panel = ui.allToolbarPanels.itemById(PANEL_ID)
        if panel and not panel.controls.itemById(CMD_ID):
            panel.controls.addCommand(cmd_def)
    except Exception:
        try:
            _fail(_ui(), "Detent failed to load")
        except Exception:
            pass


def stop(context):
    try:
        ui = _ui()
        panel = ui.allToolbarPanels.itemById(PANEL_ID)
        if panel:
            ctrl = panel.controls.itemById(CMD_ID)
            if ctrl:
                ctrl.deleteMe()
        cmd_def = ui.commandDefinitions.itemById(CMD_ID)
        if cmd_def:
            cmd_def.deleteMe()
        _handlers.clear()
    except Exception:
        try:
            _fail(_ui(), "Detent failed to unload")
        except Exception:
            pass
