"""Detent add-in: Utilities > ADD-INS > Sync Library.

Dialogs and progress only. The sync logic is in detent_core/, which is tested
without Fusion.
"""

import os
import sys
import traceback

import adsk.core
import adsk.fusion

VERSION = "0.4.2"

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# Fusion reruns this file on Run but keeps imported modules, so an update
# wouldn't load until Fusion restarts. Remove the package so it re-imports.
for _stale in [n for n in list(sys.modules)
               if n == "detent_core" or n.startswith("detent_core.")]:
    del sys.modules[_stale]

from detent_core import VERSION as CORE_VERSION  # noqa: E402
from detent_core.build import build_id, version_line  # noqa: E402
from detent_core import config as C            # noqa: E402
from detent_core import github as gh           # noqa: E402
from detent_core import sync as S              # noqa: E402
from detent_core import manifest as M          # noqa: E402
from detent_core import fusion_project as FP   # noqa: E402
from detent_core.datapanel import FusionDataPanel  # noqa: E402

CMD_ID = "DetentSyncLibrary"
CMD_NAME = "Sync Library"
CMD_TIP = ("Sync a Git-hosted CAD library into this project. Downloads only "
           "what changed. Never modifies a file it has already placed.")
PANEL_ID = "SolidScriptsAddinsPanel"    # Utilities tab > ADD-INS
LARGE_SYNC = 100                        # files; above this, suggest a smaller test first

CONFIG_PATH = os.path.join(_HERE, "config.json")
STATE_DIR = os.path.join(_HERE, "state")
LOG_PATH = os.path.join(STATE_DIR, "detent.log")
# The code Fusion loaded. Compared with the files on disk each time the
# dialog opens.
LOADED_BUILD = build_id(_HERE)

_handlers = []      # keep references, or Fusion drops the handlers


def _log(text):
    """Write the full error to state/detent.log. Message boxes cut it short."""
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


def _done(progress):
    """Close a progress dialog. doEvents() makes it actually disappear before
    the next message box opens."""
    try:
        progress.hide()
        adsk.doEvents()
    except Exception:                                  # noqa: BLE001
        pass


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
            action.listItems.add("Sync now", True, "")
            action.listItems.add("Preview changes", False, "")
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
    """Update Folder and the note when a different library is picked."""

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


def _version_line() -> str:
    """The version and build Fusion is running. Warns if it's out of date."""
    return version_line(VERSION, CORE_VERSION, LOADED_BUILD, build_id(_HERE))


def _config_note(source) -> str:
    if source is None:
        return f"No sources configured. Edit:\n{CONFIG_PATH}"
    return (f"{source.repo} @ {source.ref}\n"
            f"Matching: {', '.join(source.include)}\n"
            f"{_version_line()}")


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

            os.makedirs(STATE_DIR, exist_ok=True)
            manifest_path = os.path.join(STATE_DIR, source.manifest_name)

            # A new folder means a second full copy of the library. Ask first.
            stranded = S.folder_change_impact(
                M.Manifest.load(manifest_path), source.folder_path, folder_path)
            if stranded and not _confirm_folder_change(
                    ui, source.folder_path, folder_path, stranded):
                return

            source.folder_path = folder_path
            # Preview changes nothing, including config.json.
            if not action.startswith("Preview"):
                cfg.save(CONFIG_PATH)

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


def _confirm_folder_change(ui, old_folder, new_folder, stranded) -> bool:
    answer = ui.messageBox(
        "The folder for this library has changed:\n\n"
        f"    from   {old_folder or '(project root)'}\n"
        f"    to     {new_folder or '(project root)'}\n\n"
        f"{stranded} file(s) are recorded in the old folder. Detent will "
        "treat every one of them as missing and upload a fresh copy into the "
        "new folder. It cannot delete the old copies, so you would have "
        "both.\n\nContinue?",
        "Detent - folder change",
        adsk.core.MessageBoxButtonTypes.YesNoButtonType)
    return answer == adsk.core.DialogResults.DialogYes


def _pick_project(name, log):
    for _hub, pname, proj in FP.list_projects(_app()):
        if pname == name:
            return proj
    return FP.resolve_project(_app(), None, log)


def _do_sync(ui, src, manifest_path, panel, transport, source, write: bool):
    """Preview first, then ask before uploading. A sync with nothing to upload
    still runs, so the manifest records the check."""
    progress = ui.createProgressDialog()
    progress.isCancelButtonShown = False
    progress.show("Detent", "Reading %v...", 0, 1)
    adsk.doEvents()
    try:
        plan, preview = S.sync(src, manifest_path, panel, transport,
                               source.include, source.exclude, dry_run=True,
                               verify_placed=source.verify_placed)
    finally:
        _done(progress)

    summary = (_plan_text(plan, source, preview) if not plan.is_empty
               else "Everything is up to date.")
    check = S.check_lines(preview.check)
    if check:
        summary += "\n\n" + "\n".join(check)
    if preview.occupied:
        # Warn now, before the upload. These are counted in the plan above.
        summary += ("\n\nNot uploadable - a file of that name is already in "
                    f"the folder and Detent did not put it there ({len(preview.occupied)}):\n")
        summary += "\n".join(f"    {_short(p)}" for p, _n in preview.occupied[:5])
        if len(preview.occupied) > 5:
            summary += f"\n    ... and {len(preview.occupied) - 5} more"
    if not write:
        ui.messageBox(summary, "Detent - preview")
        return

    # Ask before uploading, counting new and missing files.
    n = S.upload_count(plan, preview)
    if n:
        warn = ""
        if n >= LARGE_SYNC:
            warn = ("\n\nThis is a big sync. Keep Fusion open until it "
                    "finishes. To try a few files first, narrow 'include' in "
                    "config.json.")
        answer = ui.messageBox(
            f"{summary}\n\nUpload {gh.estimate(n, preview.download_bytes)}?{warn}",
            "Detent", adsk.core.MessageBoxButtonTypes.YesNoButtonType)
        if answer != adsk.core.DialogResults.DialogYes:
            return

    progress = ui.createProgressDialog()
    progress.isCancelButtonShown = True
    # Sized by every file being sent, including missing ones.
    progress.show("Detent", "Starting...", 0, max(len(plan.to_place), 1))

    def on_progress(i, total, label):
        # Returning False cancels the sync.
        if progress.wasCancelled:
            return False
        if label:
            progress.message = label.replace("%", "%%")
        progress.progressValue = min(max(i, 0), total)
        adsk.doEvents()
        return True

    try:
        # Same commit as the preview, in case the branch moved since.
        _plan2, report = S.sync(src, manifest_path, panel, transport,
                                source.include, source.exclude,
                                dry_run=False, on_progress=on_progress,
                                verify_placed=source.verify_placed,
                                commit=plan.commit)
    except gh.Cancelled:
        _done(progress)
        ui.messageBox("Cancelled. Nothing was left half-written - rerun to "
                      "pick up where it stopped.", "Detent")
        return
    finally:
        _done(progress)

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
        _done(progress)

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


def _plan_text(plan, source, preview) -> str:
    at = f" ({plan.commit[:7]})" if plan.commit else ""
    lines = [f"{source.repo} @ {source.ref}{at}", ""]
    lines.append(f"  + {len(plan.add):5d}  to add")
    if plan.change:
        lines.append(f"  ~ {len(plan.change):5d}  changed upstream (Phase 2)")
    if plan.missing:
        lines.append(f"  * {len(plan.missing):5d}  MISSING from the Data Panel, will re-place")
    if plan.conflict:
        lines.append(f"  X {len(plan.conflict):5d}  MISSING but the name is taken - needs a human")
    if plan.orphan:
        lines.append(f"  - {len(plan.orphan):5d}  gone upstream (cannot delete)")
    if plan.unverified:
        lines.append(f"  ? {len(plan.unverified):5d}  unverified")
    if plan.inflight:
        lines.append(f"  ! {len(plan.inflight):5d}  not confirmed yet, Sync now checks them")
    if plan.change:
        # Detent can't update these, so list them for the user.
        lines += ["", "Changed upstream - Detent cannot update these. Replace "
                      "them by hand if you want the new versions:"]
        lines += [f"    {_short(p)}" for p in plan.change[:8]]
        if len(plan.change) > 8:
            lines.append(f"    ... and {len(plan.change) - 8} more")

    place = plan.to_place
    if place:
        lines += ["", "Estimated: " + gh.estimate(S.upload_count(plan, preview),
                                                 preview.download_bytes)]
        lines += ["", "First few:"]
        lines += [f"    {_short(p)}" for p in place[:8]]
        if len(place) > 8:
            lines.append(f"    ... and {len(place) - 8} more")
    return "\n".join(lines)


def _short(path: str, width: int = 58) -> str:
    """Shorten a long path to its end, which is the part that names the file."""
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
    if report.occupied:
        lines += ["", "Not uploaded - that name is already in the folder and "
                      "Detent did not place it:"]
        lines += [f"  {_short(p)}" for p, _n in report.occupied[:8]]
        if len(report.occupied) > 8:
            lines.append(f"  ... and {len(report.occupied) - 8} more")
        lines.append("  Adopt the library, or rename/remove the file that is there.")
    if report.conflicts:
        lines += ["", "Blocked - recorded as placed, gone, and the name is taken:"]
        lines += [f"  {_short(p)}" for p, _m in report.conflicts[:8]]
        lines += ["  Delete the impostor, or re-Adopt the library."]
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

        # Warn at load if Fusion kept an old copy of detent_core.
        if CORE_VERSION != VERSION:
            ui.messageBox(
                f"Detent {VERSION} is running against a cached "
                f"detent_core {CORE_VERSION}.\n\n"
                "Fusion kept the old modules in memory. Restart Fusion to "
                "pick up the update - Stop/Run is not enough.", "Detent")
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
