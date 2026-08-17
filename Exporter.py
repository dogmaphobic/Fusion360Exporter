import adsk.core
import adsk.drawing
import traceback
from pathlib import Path
from datetime import datetime
from typing import NamedTuple, List, Set, Dict
from enum import Enum, StrEnum
from dataclasses import dataclass
import hashlib
import re
from collections import defaultdict
import itertools
import json
import os
import sys
import threading
from functools import partial
import zipfile
import base64

# Fusion executes a Script entry point without reliably adding the Script's
# directory to sys.path.  Make adjacent support modules importable explicitly.
SCRIPT_DIRECTORY = Path(__file__).resolve().parent
if str(SCRIPT_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIRECTORY))

from resilience import (
    ExportJournal,
    MemoryMonitor,
    bytes_as_gib,
    output_is_fresh,
    partial_output_path,
)

# Older versions of this script used '_' as seperator but Fusion 360 uses ' ' per default in manual exports.
VERSION_SEPARATOR = '_' # use either ' ' or '_'

log_file = None
log_fh = None
active_job = None
process_event = None
event_pump = None
PROCESS_EVENT_ID = 'dogmaphobic_Fusion360Exporter_ProcessNextFile'

handlers = []
# map from presentation of `project/folder` shown in UI to (project id, folder id)
# and also from `project` to (project id, None) if subfolders not enabled
# this is kinda hacky but not sure how reliable keying on the list item itself is
project_folders_d = {} # {f'{project.name}/{folder.name}': (project.id, folder.id)}

last_settings_path = Path(__file__).parent / 'last_settings.json'

def log(*args):
    print(*args, file=log_fh)
    log_fh.flush()

def init_directory(name):
    directory = Path(name)
    directory.mkdir(exist_ok=True, parents=True)
    return directory

def init_logging(directory):
    global log_file, log_fh
    log_file = directory / '{:%Y_%m_%d_%H_%M_%S}.txt'.format(datetime.now())
    log_fh = open(log_file, 'w', encoding="utf-8")

def load_last_settings():
    if not last_settings_path.exists():
        return {}
    with open(last_settings_path) as fh:
        return json.load(fh)

def save_last_settings(d):
    with open(last_settings_path, 'w') as fh:
        json.dump(d, fh, indent=2)

# Having f3d first dictates the order we process calls to export_file, and we want f3d first so that
# things aren't unhidden when we take the thumbnail
class Format(Enum):
    F3D = 'f3d'
    STEP = 'step'
    STL = 'stl'
    IGES = 'igs'
    SAT = 'sat'
    SMT = 'smt'
    TMF = '3mf'
    PDF = 'pdf'

FormatFromName = {x.value: x for x in Format}

DEFAULT_SELECTED_FORMATS = {Format.F3D.value, Format.STEP.value}

archive_extensions = ['.zip', '.rar', '.gz', '.tar.gz', '.tar.bz2', '.tar.xz']

class Ctx(NamedTuple):
    app: adsk.core.Application
    folder: Path
    formats: List[Format]
    projects_folders: Dict[str, List[str]] # {projectId: [folderId+]} empty list is taken to mean "no filter"
    use_active_folder: bool
    unhide_all: bool
    save_sketches: bool
    num_versions: int # -1 means all versions
    export_non_design_files: bool
    retry_quarantined: bool = False
    minimum_free_memory_gib: int = 4

    def extend(self, other):
        return self._replace(folder=self.folder / other)

    def to_dict(self):
        d = self._asdict()
        d.pop('app')
        d['folder'] = str(d['folder'])
        d['formats'] = [x.value for x in d['formats']]
        d['projects_folders'] = {k: list(v) for k, v in d['projects_folders'].items()}
        return d

    def dumps(self):
        return json.dumps(self.to_dict(), indent=2)

    @classmethod
    def from_dict(cls, d, app):
        d = dict(d)
        d['app'] = app
        d['folder'] = Path(d['folder'])
        d['formats'] = [FormatFromName[x.lower()] for x in d['formats']]
        d['projects_folders'] = {k: set(v) for k, v in d['projects_folders'].items()}
        d.setdefault('retry_quarantined', False)
        d.setdefault('minimum_free_memory_gib', 4)
        d.setdefault('use_active_folder', False)
        d.setdefault('export_non_design_files', False)
        return cls(**d)

    def has_show_folders(self):
        return any(len(v) > 0 for v in self.projects_folders.values())

class LazyDocument:
    def __init__(self, ctx: Ctx, file: adsk.core.DataFile):
        self._ctx = ctx
        self._document = None
        self.file = file
        self.unhidden = False
        self._open_attempted = False
        self._open_error = None

    def open(self):
        if self._document is not None:
            return
        if self._open_attempted:
            raise RuntimeError(
                f'Previous attempt to open `{self.file.name}` v{self.file.versionNumber} failed'
            ) from self._open_error
        self._open_attempted = True
        log(f'Opening `{self.file.name}` v{self.file.versionNumber}')
        previous_document = self._ctx.app.activeDocument
        try:
            self._document = self._ctx.app.documents.open(self.file)
            if self._document is None:
                raise RuntimeError('Fusion returned no document')
            if self._document.activate() is False:
                raise RuntimeError('Fusion could not activate the opened document')
        except Exception as exc:
            self._open_error = exc
            if self._document is None:
                try:
                    candidate = self._ctx.app.activeDocument
                    if (
                        candidate is not None
                        and candidate is not previous_document
                        and str(candidate.dataFile.versionId) == str(self.file.versionId)
                    ):
                        self._document = candidate
                except Exception:
                    pass
            raise

    def unhide_all(self):
        if self.unhidden:
            return
        unhide_all_in_document(self._document)
        self.unhidden = True

    def close(self):
        if self._document is None:
            return
        log(f'Closing `{self.file.name}` v{self.file.versionNumber}')
        if self._document.close(False) is False:  # don't save changes
            raise DocumentCloseError(
                f'Fusion could not close `{self.file.name}` v{self.file.versionNumber}'
            )
        self._document = None

    @property
    def design(self):
        return design_from_document(self._document)

    @property
    def rootComponent(self):
        return self.design.rootComponent

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class ControlledStop(Exception):
    """Base class for a deliberate, clean stop of the export job."""


class UserCancelled(ControlledStop):
    pass


class DocumentCloseError(ControlledStop):
    pass

@dataclass
class Counter:
    saved: int = 0
    skipped: int = 0
    errored: int = 0
    quarantined: int = 0

    def __add__(self, other):
        return Counter(
            self.saved + other.saved,
            self.skipped + other.skipped,
            self.errored + other.errored,
            self.quarantined + other.quarantined,
        )
    def __iadd__(self, other):
        self.saved += other.saved
        self.skipped += other.skipped
        self.errored += other.errored
        self.quarantined += other.quarantined
        return self

def design_from_document(document: adsk.core.Document):
    return adsk.fusion.FusionDocument.cast(document).design

def unhide_all_in_document(document: adsk.core.Document):
    unhide_all_in_component(design_from_document(document).rootComponent)

def unhide_all_in_component(component):
    component.isBodiesFolderLightBulbOn = True
    component.isSketchFolderLightBulbOn = True

    for brep in component.bRepBodies:
        brep.isLightBulbOn = True

    for body in component.meshBodies:
        body.isLightBulbOn = True

    # I find the name occurrences very confusing, but apparently that is what a sub-component is called
    for occurrence in component.occurrences:
        occurrence.isLightBulbOn = True
        unhide_all_in_component(occurrence.component)

def sanitize_filename(name: str) -> str:
    """
    Remove "bad" characters from a filename. Right now just punctuation that Windows doesn't like
    If any chars are removed, we append _{hash} so that we don't accidentally clobber other files
    since eg `Model 1/2` and `Model 1 2` would otherwise have the same name
    """
    # this list of characters is just from trying to rename a file in Explorer (on Windows)
    # I think the actual requirements are per fileystem and will be different on Mac
    # I'm not sure how other unicode chars are handled
    with_replacement = re.sub(r'[:\\/*?<>|"]', ' ', name)
    if name == with_replacement:
        return name
    log(f'filename `{name}` contained bad chars, replacing by `{with_replacement}`')
    hash = hashlib.sha256(name.encode()).hexdigest()[:8]
    return f'{with_replacement}_{hash}'

def set_mtime(path: Path, time: int):
    """utime wants to set atime and mtime, we just set it the same"""
    os.utime(path, (time, time))

def output_path_exists(path: Path, file: adsk.core.DataFile) -> bool:
    """
    Check whether a non-empty output (or archived output) is at least as new as
    the cloud data file. Stale outputs are deliberately re-exported.
    """
    if output_is_fresh(path, file.dateModified):
        log(f'{path} is up to date, skipping')
        return True
    if path.exists():
        log(f'{path} exists but is stale or empty, replacing')

    for archive_extension in archive_extensions:
        archive_path = path.with_name(path.name + archive_extension)
        if output_is_fresh(archive_path, file.dateModified):
            log(f'{path} has an up-to-date archive at {archive_path}, skipping')
            return True
        if archive_path.exists():
            log(f'{archive_path} exists but is stale or empty')

    return False


def atomic_output(output_path: Path, source_mtime: int, write_partial, postprocess=None):
    """Write beside the target and publish it only after successful validation."""
    output_path.parent.mkdir(exist_ok=True, parents=True)
    partial_path = partial_output_path(output_path)
    try:
        if partial_path.exists():
            partial_path.unlink()
        result = write_partial(partial_path)
        if result is False:
            raise RuntimeError(f'Fusion reported that writing {output_path} failed')
        if not partial_path.exists() or partial_path.stat().st_size == 0:
            raise RuntimeError(f'Fusion did not create a non-empty {output_path.suffix} file')
        if postprocess is not None:
            postprocess(partial_path)
        os.replace(partial_path, output_path)
        set_mtime(output_path, source_mtime)
    except Exception:
        try:
            if partial_path.exists():
                partial_path.unlink()
        except OSError:
            pass
        raise

# component: adsk.core.Component but that doesn't exist for some reason?
# sketch   : adsk.core.Sketch likewise
def export_sketch(ctx: Ctx, doc: LazyDocument, component, sketch):
    output_path = ctx.folder / f'{sanitize_filename(sketch.name)}.dxf'
    if output_path_exists(output_path, doc.file):
        return Counter(skipped=1)

    log(f'Exporting sketch {sketch.name} in {component.name} to {output_path}')
    atomic_output(
        output_path,
        doc.file.dateModified,
        lambda partial_path: sketch.saveAsDXF(str(partial_path)),
    )
    return Counter(saved=1)

def visit_sketches(ctx: Ctx, doc: LazyDocument, component, control=None):
    counter = Counter()
    for sketch in component.sketches:
        try:
            check_control(control)
            counter += export_sketch(ctx, doc, component, sketch)
        except ControlledStop:
            raise
        except Exception:
            log(traceback.format_exc())
            counter.errored += 1

    for occurrence in component.occurrences:
        check_control(control)
        counter += visit_sketches(
            ctx.extend(sanitize_filename(occurrence.name)),
            doc,
            occurrence.component,
            control,
        )

    return counter

def tree_gen(file: adsk.core.DataFile) -> str:
    folders = []
    df = file
    while True:
        if not df.parentFolder:
            break
        folders.append(df.parentFolder.name)
        df = df.parentFolder

    folders.reverse()
    return '\\'.join(folders)

def export_filename(ctx: Ctx, file: adsk.core.DataFile, format: Format=None):
    extension = file.fileExtension if format is None else format.value
    sanitized = sanitize_filename(file.name)
    name = f'{sanitized}{VERSION_SEPARATOR}v{file.versionNumber}.{extension}'
    return ctx.folder / name

def export_file(ctx: Ctx, format: Format, doc: LazyDocument, check_existing=True) -> Counter:
    output_path = export_filename(ctx, doc.file, format)
    if check_existing and output_path_exists(output_path, doc.file):
        return Counter(skipped=1)

    doc.open()

    design = doc.design
    em = design.exportManager

    # f3d already saves everything that is hidden and for the thumbnail to look nice, we don't want to unhide everything
    # Note that because unhiding is a mutation, the order of calls to export_file matters, but f3d will be first
    if ctx.unhide_all and format != Format.F3D:
        doc.unhide_all()

    def write_partial(partial_path):
        partial_path_s = str(partial_path)
        if format == Format.F3D:
            options = em.createFusionArchiveExportOptions(partial_path_s)
        elif format == Format.STL:
            options = em.createSTLExportOptions(design.rootComponent, partial_path_s)
        elif format == Format.TMF:
            options = em.createC3MFExportOptions(design.rootComponent, partial_path_s)
        elif format == Format.STEP:
            options = em.createSTEPExportOptions(partial_path_s)
        elif format == Format.IGES:
            options = em.createIGESExportOptions(partial_path_s)
        elif format == Format.SAT:
            options = em.createSATExportOptions(partial_path_s)
        elif format == Format.SMT:
            options = em.createSMTExportOptions(partial_path_s)
        else:
            raise ValueError(f'Got unknown export format {format}')
        if options is None:
            raise RuntimeError(f'Fusion could not create export options for {format.value}')
        return em.execute(options)

    def add_thumbnail(partial_path):
        if format != Format.F3D:
            return
        try:
            thumb_b64 = design.rootComponent.createThumbnail(256, 256, 'PNG').getAsBase64String()
            with zipfile.ZipFile(partial_path, 'a') as zf:
                with zf.open('FusionAssetName[Active]/Previews/small.png', 'w') as fh:
                    fh.write(base64.b64decode(thumb_b64))
        except Exception:
            log(f'WARNING: could not add thumbnail to {output_path}\n{traceback.format_exc()}')

    atomic_output(output_path, doc.file.dateModified, write_partial, add_thumbnail)
    log(f'Saved {output_path}')

    return Counter(saved=1)

def export_drawing(ctx: Ctx, format: Format, doc: LazyDocument, check_existing=True) -> Counter:
    output_path = export_filename(ctx, doc.file, format)
    if check_existing and output_path_exists(output_path, doc.file):
        return Counter(skipped=1)

    doc.open()

    drawing = adsk.drawing.Drawing.cast(ctx.app.activeProduct)
    em: adsk.drawing.DrawingExportManager = drawing.exportManager

    def write_partial(partial_path):
        options = em.createPDFExportOptions(str(partial_path))
        if options is None:
            raise RuntimeError('Fusion could not create PDF export options')
        return em.execute(options)

    atomic_output(output_path, doc.file.dateModified, write_partial)
    log(f'PDF created {output_path}')
    log(f'Saved {output_path}')

    return Counter(saved=1)


def check_control(control):
    if control is not None:
        control.check()


def visit_file(ctx: Ctx, file: adsk.core.DataFile, control=None) -> Counter:
    log(f'Visiting file {file.name} v{file.versionNumber}.{file.fileExtension}')

    counter = Counter()

    if file.fileExtension != 'f3d' and file.fileExtension != 'f2d':
        if not ctx.export_non_design_files:
            log(f'Skipping non-design file {file.name} with extension {file.fileExtension}')
            counter.skipped += 1
            return counter

        log(f'file {file.name} has extension {file.fileExtension} attempting direct download')

        try:
            output_path = export_filename(ctx, file)
            if output_path_exists(output_path, file):
                counter.skipped += 1
                return counter

            atomic_output(
                output_path,
                file.dateModified,
                lambda partial_path: file.download(str(partial_path), None),
            )
            log(f'Saved {output_path}')
            counter.saved += 1

        except Exception:
            counter.errored += 1
            log(traceback.format_exc())

        return counter

    with LazyDocument(ctx, file) as doc:
        drawing_needed = False
        if file.fileExtension == 'f2d' and Format.PDF in ctx.formats:
            if output_path_exists(export_filename(ctx, file, Format.PDF), file):
                counter.skipped += 1
            else:
                drawing_needed = True
        formats_needed = []
        if file.fileExtension == 'f3d':
            for format in ctx.formats:
                if format == Format.PDF:
                    continue
                if output_path_exists(export_filename(ctx, file, format), file):
                    counter.skipped += 1
                else:
                    formats_needed.append(format)

        needs_open = (
            (ctx.save_sketches and file.fileExtension != 'f2d')
            or drawing_needed
            or bool(formats_needed)
        )
        if needs_open:
            check_control(control)
            try:
                doc.open()
            except Exception:
                counter.errored += 1
                log(
                    f'ERROR opening `{file.name}` v{file.versionNumber}; skipping this model\n'
                    f'{traceback.format_exc()}'
                )
                return counter

        if ctx.save_sketches and file.fileExtension != 'f2d':
            check_control(control)
            counter += visit_sketches(
                ctx.extend(sanitize_filename(doc.rootComponent.name)),
                doc,
                doc.rootComponent,
                control,
            )

        if drawing_needed:
            try:
                check_control(control)
                counter += export_drawing(ctx, Format.PDF, doc, check_existing=False)
            except ControlledStop:
                raise
            except Exception:
                counter.errored += 1
                log(traceback.format_exc())

        elif file.fileExtension == 'f3d':
            for format in formats_needed:
                try:
                    check_control(control)
                    counter += export_file(ctx, format, doc, check_existing=False)
                except ControlledStop:
                    raise
                except Exception:
                    counter.errored += 1
                    log(traceback.format_exc())

    return counter

def file_versions(file: adsk.core.DataFile, num_versions):
    # file.versions (should) start with the current/latest version
    # we discovered that file.versions is actually sorted by the string of the versionNumber
    # so for something with 11 versions, we get [9, 8, 7, 6, 5, 4, 3, 2, 11, 10, 1]
    # but versionNumber does appear to always be an int so far, not sure where that error creeps in
    # so we just have to resort by int
    # it's possible this is not ideal for very large version counts if the swig layer is actually lazy
    # and so we force the iterator, but not sure, and idk how to avoid it and still get the versions in the
    # right order.
    versions = sorted(file.versions, key=lambda x: x.versionNumber, reverse=True)

    if versions[0].versionNumber != file.versionNumber:
        raise Exception(f'Expected versions[0] to be current file version, but got {versions[0].versionNumber}')

    if num_versions == -1:
        versions = versions[1:]
    else:
        versions = versions[1:num_versions+1]

    yield file
    prev = file.versionNumber
    for v in versions:
        if prev - v.versionNumber != 1:
            raise Exception(f'Versions not contiguous! prev={prev} cur={v.versionNumber}')
        yield v
        prev = v.versionNumber

def visit_folder(ctx: Ctx, folder, recurse=True, control=None) -> Counter:
    log(f'Visiting folder {folder.name}')

    new_ctx = ctx.extend(sanitize_filename(folder.name))

    counter = Counter()

    for file in folder.dataFiles:
        try:
            for file_version in file_versions(file, ctx.num_versions):
                check_control(control)
                counter += visit_file(new_ctx, file_version, control)
        except ControlledStop:
            raise
        except Exception:
            log(f'Got exception visiting file\n{traceback.format_exc()}')
            counter.errored += 1

    if recurse:
        for sub_folder in folder.dataFolders:
            check_control(control)
            counter += visit_folder(new_ctx, sub_folder, control=control)

    return counter

def main(ctx: Ctx) -> Counter:
    init_directory(ctx.folder)
    init_logging(ctx.folder)

    log(ctx.dumps())

    counter = Counter()

    if ctx.use_active_folder:
        root_folder = ctx.app.data.activeFolder
        tree_buffer = tree_gen(root_folder)
        new_ctx = ctx.extend(Path(tree_buffer))
        counter += visit_folder(new_ctx, ctx.app.data.activeFolder)
    else:
        for project_id, folder_ids in ctx.projects_folders.items():
            project = ctx.app.data.dataProjects.itemById(project_id)

            if not folder_ids:  # empty filter visits everything
                counter += visit_folder(ctx, project.rootFolder)

            # if the root folder is the only thing selected, we take that to mean no recurse
            elif set(folder_ids) == {project.rootFolder.id}:
                counter += visit_folder(ctx, project.rootFolder, recurse=False)

            else:
                folders = project.rootFolder.dataFolders
                # hmm this doesn't work, the itemsById doesn't return the folder
                # for folder_id in folder_ids:
                #     counter += visit_folder(ctx, folders.itemById(folder_id))
                for folder in filter(lambda x: x.id in folder_ids, folders):
                    counter += visit_folder(ctx, folder)

    return counter


@dataclass(frozen=True)
class FileWorkItem:
    ctx: Ctx
    file: object

    @property
    def key(self):
        try:
            return str(self.file.versionId)
        except Exception:
            return f'{self.file.id}:v{self.file.versionNumber}'

    def journal_record(self):
        return {
            'key': self.key,
            'name': str(self.file.name),
            'version': int(self.file.versionNumber),
            'extension': str(self.file.fileExtension),
            'output_folder': str(self.ctx.folder),
        }


def collect_folder_work(ctx: Ctx, folder, recurse, items, counter):
    log(f'Collecting folder {folder.name}')
    new_ctx = ctx.extend(sanitize_filename(folder.name))
    try:
        files = [file for file in folder.dataFiles]
    except Exception:
        log(f'ERROR collecting files beneath {folder.name}\n{traceback.format_exc()}')
        counter.errored += 1
        files = []

    for file in files:
        try:
            for file_version in file_versions(file, ctx.num_versions):
                items.append(FileWorkItem(new_ctx, file_version))
        except Exception:
            log(f'ERROR collecting versions for {getattr(file, "name", "?")}\n{traceback.format_exc()}')
            counter.errored += 1

    if not recurse:
        return
    try:
        subfolders = [subfolder for subfolder in folder.dataFolders]
    except Exception:
        log(f'ERROR collecting subfolders beneath {folder.name}\n{traceback.format_exc()}')
        counter.errored += 1
        return
    for subfolder in subfolders:
        try:
            collect_folder_work(new_ctx, subfolder, True, items, counter)
        except Exception:
            log(f'ERROR collecting folder {getattr(subfolder, "name", "?")}\n{traceback.format_exc()}')
            counter.errored += 1


def collect_work_items(ctx: Ctx):
    items = []
    counter = Counter()
    if ctx.use_active_folder:
        root_folder = ctx.app.data.activeFolder
        tree_buffer = tree_gen(root_folder)
        collect_folder_work(ctx.extend(Path(tree_buffer)), root_folder, True, items, counter)
    else:
        for project_id, folder_ids in ctx.projects_folders.items():
            try:
                project = ctx.app.data.dataProjects.itemById(project_id)
                if project is None:
                    raise RuntimeError(f'Project {project_id} no longer exists')
                if not folder_ids:
                    collect_folder_work(ctx, project.rootFolder, True, items, counter)
                elif set(folder_ids) == {project.rootFolder.id}:
                    collect_folder_work(ctx, project.rootFolder, False, items, counter)
                else:
                    wanted = set(folder_ids)
                    for folder in project.rootFolder.dataFolders:
                        if folder.id in wanted:
                            collect_folder_work(ctx, folder, True, items, counter)
            except Exception:
                log(f'ERROR collecting project {project_id}\n{traceback.format_exc()}')
                counter.errored += 1
    return items, counter


class ExportJobControl:
    def __init__(self, job):
        self.job = job

    def check(self):
        if self.job.stop_requested or self.job.was_cancelled():
            raise UserCancelled('Export stopped at user request')


class ExportJob:
    def __init__(self, ctx: Ctx):
        init_directory(ctx.folder)
        # Construct recovery state before opening this run's new log so a
        # pre-journal crash can be recognized from the previous log.
        self.journal = ExportJournal(ctx.folder)
        init_logging(ctx.folder)
        log(ctx.dumps())
        self.ctx = ctx
        self.ui = ctx.app.userInterface
        self.counter = Counter()
        self.items = []
        self.index = 0
        self.prepared = False
        self.finished = False
        self.step_active = False
        self.stop_reason = None
        self.stop_requested = False
        self.progress = None
        self.control = ExportJobControl(self)
        self.memory_monitor = MemoryMonitor(ctx.minimum_free_memory_gib, 0.10)
        self.last_memory = None
        if self.journal.load_error:
            log(f'WARNING: {self.journal.load_error}')
        if self.journal.recovered_record:
            record = self.journal.recovered_record
            log(
                f'QUARANTINED after interrupted run: {record.get("name", "?")} '
                f'v{record.get("version", "?")} ({record.get("key", "?")})'
            )

    def prepare(self):
        self.progress = self.ui.createProgressDialog()
        self.progress.cancelButtonText = 'Stop after current model'
        self.progress.isCancelButtonShown = True
        self.progress.isBackgroundTranslucent = False
        self.progress.show('Fusion 360 Exporter', 'Collecting cloud files...', 0, 1, 0)
        self.items, collection_counter = collect_work_items(self.ctx)
        self.counter += collection_counter
        self.progress.maximumValue = max(1, len(self.items))
        self.prepared = True
        log(f'Collected {len(self.items)} file version(s)')

    def step(self):
        if not self.prepared:
            self.prepare()
            if not self.items:
                self.stop_reason = 'completed'
                return False
            return True
        if self.stop_requested or self.was_cancelled():
            self.stop_reason = 'cancelled'
            return False
        snapshot = self.sample_memory()
        if snapshot is None:
            self.stop_reason = 'memory monitor'
            return False
        if self.memory_monitor.is_low(snapshot):
            self.stop_reason = 'memory'
            floor = self.memory_monitor.safety_floor(snapshot)
            log(f'STOPPING for memory pressure: {snapshot.describe()}; floor {bytes_as_gib(floor)}')
            return False
        if self.index >= len(self.items):
            self.stop_reason = 'completed'
            return False

        item = self.items[self.index]
        self.progress.message = (
            f'File %v of %m\n{item.file.name} v{item.file.versionNumber}\n{snapshot.describe()}'
        )
        self.process_item(item)
        self.index += 1
        self.progress.progressValue = self.index

        if self.stop_reason in ('cancelled', 'close failure', 'journal failure'):
            return False
        snapshot = self.sample_memory()
        if snapshot is None:
            self.stop_reason = 'memory monitor'
            return False
        if self.memory_monitor.is_low(snapshot):
            self.stop_reason = 'memory'
            floor = self.memory_monitor.safety_floor(snapshot)
            log(f'STOPPING after file for memory pressure: {snapshot.describe()}; floor {bytes_as_gib(floor)}')
            return False
        if self.stop_requested or self.was_cancelled():
            self.stop_reason = 'cancelled'
            return False
        if self.index >= len(self.items):
            self.stop_reason = 'completed'
            return False
        return True

    def process_item(self, item):
        record = item.journal_record()
        if self.journal.is_quarantined(item.key, record):
            if self.ctx.retry_quarantined:
                log(f'Retrying quarantined file {item.file.name} v{item.file.versionNumber}')
                self.journal.retry(item.key, record)
            else:
                log(f'Skipping quarantined file {item.file.name} v{item.file.versionNumber}')
                self.counter.quarantined += 1
                self.counter.errored += 1
                return

        self.journal.begin(record)
        safe_to_clear_journal = True
        try:
            self.counter += visit_file(item.ctx, item.file, self.control)
        except UserCancelled:
            self.stop_reason = 'cancelled'
        except DocumentCloseError:
            safe_to_clear_journal = False
            self.counter.errored += 1
            self.stop_reason = 'close failure'
            log(f'ERROR closing file; stopping safely\n{traceback.format_exc()}')
        except Exception:
            self.counter.errored += 1
            log(f'ERROR processing file\n{traceback.format_exc()}')
        finally:
            if safe_to_clear_journal:
                try:
                    self.journal.finish()
                except Exception:
                    self.counter.errored += 1
                    self.stop_reason = 'journal failure'
                    log(f'ERROR clearing recovery journal\n{traceback.format_exc()}')

    def sample_memory(self):
        try:
            self.last_memory = self.memory_monitor.sample()
            return self.last_memory
        except Exception:
            self.counter.errored += 1
            log(f'ERROR monitoring memory\n{traceback.format_exc()}')
            return None

    def was_cancelled(self):
        try:
            return self.progress is not None and bool(self.progress.wasCancelled)
        except Exception:
            return False

    def request_stop(self):
        self.stop_requested = True

    def finish(self, reason=None):
        global log_fh
        if self.finished:
            return
        self.finished = True
        if reason:
            self.stop_reason = reason
        if not self.stop_reason:
            self.stop_reason = 'completed'
        if self.progress is not None:
            try:
                self.progress.hide()
            except Exception:
                log(f'ERROR hiding progress dialog\n{traceback.format_exc()}')

        status = {
            'completed': 'Export completed',
            'cancelled': 'Export stopped at your request',
            'memory': 'Export stopped before memory pressure became unsafe',
            'memory monitor': 'Export stopped because memory could not be monitored safely',
            'close failure': 'Export stopped because Fusion could not close a document',
            'journal failure': 'Export stopped because recovery state could not be saved safely',
            'fatal error': 'Export stopped after an unrecoverable runner error',
        }.get(self.stop_reason, f'Export stopped: {self.stop_reason}')
        lines = [
            status,
            f'Processed {self.index} of {len(self.items)} file versions',
            f'Saved {self.counter.saved} files',
            f'Skipped {self.counter.skipped} current files',
            f'Skipped {self.counter.quarantined} quarantined models',
            f'Encountered {self.counter.errored} errors',
        ]
        if self.last_memory is not None:
            lines.append(f'Final memory: {self.last_memory.describe()}')
        if self.stop_reason == 'memory':
            lines.append('Restart Fusion before resuming the export.')
        elif self.stop_reason == 'close failure':
            lines.append('Close the remaining document or restart Fusion before resuming.')
        lines.append(f'Log file is at {log_file}')
        summary = '\n'.join(lines)
        log(summary)
        if log_fh is not None:
            log_fh.close()
            log_fh = None
        self.ui.messageBox(summary)

def message_box_traceback():
    adsk.core.Application.get().userInterface.messageBox(traceback.format_exc())


class CustomEventPump(threading.Thread):
    """Queue custom events from the worker thread expected by Fusion's API."""

    def __init__(self, app, dispatch_delay=0.01):
        super().__init__(name='Fusion360ExporterEventPump', daemon=True)
        self.app = app
        self.dispatch_delay = dispatch_delay
        self.requested = threading.Event()
        self.stopping = threading.Event()

    def request(self):
        self.requested.set()

    def shutdown(self):
        self.stopping.set()
        self.requested.set()

    def run(self):
        while not self.stopping.is_set():
            self.requested.wait()
            self.requested.clear()
            if self.stopping.is_set():
                return
            # Let the Fusion event handler which requested this dispatch return
            # before asking Fusion for another callback.
            if self.stopping.wait(self.dispatch_delay):
                return
            try:
                # Fire exactly once. Fusion has been observed to enqueue the
                # callback even when this method reports False. Retrying can
                # therefore flood the queue and re-enter a blocking document
                # open hundreds of times.
                self.app.fireCustomEvent(PROCESS_EVENT_ID, '{}')
            except Exception:
                # A Python exception means no reliable main-thread reporting
                # path remains. The next explicit request gets another chance.
                pass


def shutdown_event_pump():
    global event_pump
    pump = event_pump
    event_pump = None
    if pump is not None:
        pump.shutdown()


def terminate_exporter():
    shutdown_event_pump()
    adsk.terminate()


class ExporterProcessEventHandler(adsk.core.CustomEventHandler):
    def notify(self, args):
        global active_job
        if active_job is None:
            return
        job = active_job
        if job.step_active:
            log('WARNING: ignored a re-entrant export event')
            return
        job.step_active = True
        queue_next = False
        should_terminate = False
        try:
            ui = job.ui
            if ui.activeCommand != 'SelectCommand':
                select_command = ui.commandDefinitions.itemById('SelectCommand')
                if select_command is not None:
                    select_command.execute()
            if job.step():
                if event_pump is None:
                    raise RuntimeError('The export event pump is not running')
                queue_next = True
            else:
                active_job = None
                job.finish()
                should_terminate = True
        except Exception:
            active_job = None
            try:
                job.counter.errored += 1
                log(f'FATAL export runner error\n{traceback.format_exc()}')
                job.finish('fatal error')
            except Exception:
                try:
                    job.ui.messageBox(f'Exporter failed while reporting an error:\n{traceback.format_exc()}')
                except Exception:
                    pass
            should_terminate = True
        finally:
            job.step_active = False

        if queue_next:
            event_pump.request()
        elif should_terminate:
            terminate_exporter()


def register_process_event(app):
    global process_event, event_pump
    shutdown_event_pump()
    try:
        app.unregisterCustomEvent(PROCESS_EVENT_ID)
    except Exception:
        pass
    process_event = app.registerCustomEvent(PROCESS_EVENT_ID)
    if process_event is None:
        raise RuntimeError('Could not register the exporter processing event')
    handler = ExporterProcessEventHandler()
    if process_event.add(handler) is False:
        raise RuntimeError('Could not attach the exporter processing handler')
    handlers.append(handler)
    event_pump = CustomEventPump(app)
    event_pump.start()


def start_export_job(ctx):
    global active_job
    if active_job is not None:
        ctx.app.userInterface.messageBox('An export job is already running.')
        return False
    if event_pump is None:
        raise RuntimeError('The export event pump is not running')
    active_job = ExportJob(ctx)
    event_pump.request()
    return True

class I(StrEnum):
    """UI input ids"""
    directory = 'directory'
    file_types = 'file_types'
    use_active_folder = 'use_active_folder'
    show_folders = 'show_folders'
    projects = 'projects'
    unhide_all = 'unhide_all'
    version_count = 'version_count'
    all_versions = 'all_versions'
    save_sketches = 'save_sketches'
    version_separator_is_space = 'version_separator_is_space'
    export_non_design_files = 'export_non_design_files'
    retry_quarantined = 'retry_quarantined'
    minimum_free_memory_gib = 'minimum_free_memory_gib'

def populate_data_projects_list(dropdown, show_folders=False, selected=None):
    app = adsk.core.Application.get()
    dropdown.listItems.clear()

    if selected is None:
        selected = []

    if show_folders:
        for project in app.data.dataProjects:
            for folder in itertools.chain([project.rootFolder], project.rootFolder.dataFolders):
                name = f'{project.name}/{folder.name}'
                project_folders_d[name] = (project.id, folder.id)
                dropdown.listItems.add(name, name in selected)
    else:
        for project in app.data.dataProjects:
            project_folders_d[project.name] = (project.id, None)
            dropdown.listItems.add(project.name, project.name in selected)

class ExporterCommandInputChangedHandler(adsk.core.InputChangedEventHandler):
    def notify(self, args):
         try:
            inputs = args.inputs
            if args.input.id == I.all_versions:
                inputs.itemById(I.version_count).isEnabled = not args.input.value
            elif args.input.id == I.use_active_folder:
                inputs.itemById(I.projects).isEnabled = not args.input.value
                inputs.itemById(I.show_folders).isEnabled = not args.input.value
            elif args.input.id == I.show_folders:
                populate_data_projects_list(inputs.itemById(I.projects), args.input.value)

         except:
            message_box_traceback()

class ExporterCommandCreatedEventHandler(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        try:
            cmd = args.command

            # http://help.autodesk.com/view/fusion360/ENU/?guid=GUID-C1BF7FBF-6D35-4490-984B-11EB26232EAD
            cmd.isExecutedWhenPreEmpted = False

            onExecute = ExporterCommandExecuteHandler()
            onDestroy = ExporterCommandDestroyHandler()
            onInputChanged = ExporterCommandInputChangedHandler()
            cmd.execute.add(onExecute)
            cmd.destroy.add(onDestroy)
            cmd.inputChanged.add(onInputChanged)
            handlers.extend([onExecute, onDestroy, onInputChanged])

            inputs = cmd.commandInputs
            last_settings = load_last_settings()

            export_folder = last_settings.get(I.directory, str(Path.home() / 'Desktop/Fusion360Export'))
            inputs.addStringValueInput(I.directory, 'Directory', export_folder)

            drop = inputs.addDropDownCommandInput(I.file_types, 'Export Types', adsk.core.DropDownStyles.CheckBoxDropDownStyle)
            selected_formats = last_settings.get(I.file_types, DEFAULT_SELECTED_FORMATS)
            for format in Format:
                drop.listItems.add(format.value, format.value in selected_formats)

            use_active_folder = last_settings.get(I.use_active_folder, False)
            inputs.addBoolValueInput(I.use_active_folder, 'Download Open Folder', True, '', use_active_folder)

            #T addBoolValueInput(id, name, checkbox?, icon, default)
            show_folders = last_settings.get(I.show_folders, False)
            inputs.addBoolValueInput(I.show_folders, 'Show Project Folders', True, '', show_folders)
            inputs.itemById(I.show_folders).isEnabled = not use_active_folder

            drop = inputs.addDropDownCommandInput(I.projects, 'Export Projects', adsk.core.DropDownStyles.CheckBoxDropDownStyle)
            projects = last_settings.get(I.projects)
            populate_data_projects_list(drop, show_folders=show_folders, selected=projects)
            inputs.itemById(I.projects).isEnabled = not use_active_folder

            unhide_all = last_settings.get(I.unhide_all, True)
            inputs.addBoolValueInput(I.unhide_all, 'Unhide All Bodies', True, '', unhide_all)

            versions_group = inputs.addGroupCommandInput('group_versions', 'Versions')
            #T addIntegerSpinnerCommand(id, name, min, max, spinStep, initialValue)
            version_count = last_settings.get(I.version_count, 0)
            versions_group.children.addIntegerSpinnerCommandInput(I.version_count, 'Number of Previous Versions', 0, 2**16-1, 1, version_count)

            all_versions = last_settings.get(I.all_versions, False)
            versions_group.children.addBoolValueInput(I.all_versions, 'Save ALL Versions', True, '', all_versions)
            inputs.itemById(I.version_count).isEnabled = not all_versions

            save_sketches = last_settings.get(I.save_sketches, False)
            inputs.addBoolValueInput(I.save_sketches, 'Save Sketches as DXF', True, '', save_sketches)

            version_separator_is_space = last_settings.get(I.version_separator_is_space, VERSION_SEPARATOR == ' ')
            inputs.addBoolValueInput(I.version_separator_is_space, 'Version Separator is Space', True, '', version_separator_is_space)

            export_non_design_files = last_settings.get(I.export_non_design_files, False)
            inputs.addBoolValueInput(I.export_non_design_files, 'Export Non-Design Files', True, '', export_non_design_files)

            retry_quarantined = last_settings.get(I.retry_quarantined, False)
            inputs.addBoolValueInput(
                I.retry_quarantined,
                'Retry Previously Interrupted Models',
                True,
                '',
                retry_quarantined,
            )

            minimum_free_memory_gib = last_settings.get(I.minimum_free_memory_gib, 4)
            inputs.addIntegerSpinnerCommandInput(
                I.minimum_free_memory_gib,
                'Minimum Free Memory (GiB)',
                1,
                128,
                1,
                minimum_free_memory_gib,
            )
        except:
            message_box_traceback()

class ExporterCommandDestroyHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        try:
            if active_job is None:
                terminate_exporter()
        except:
            message_box_traceback()

# Dont use yield and don't copy list items, swig wants to delete things
def selected(inputs):
    return [it.name for it in inputs if it.isSelected]

def make_projects_folders(inputs):
    ret = defaultdict(set)
    for it in inputs.itemById(I.projects).listItems:
        if it.isSelected:
            project_id, folder_id = project_folders_d[it.name]
            if folder_id is None:  # whole project was selected
                ret[project_id] = []
            else:
                ret[project_id].add(folder_id)
    return ret

def run_main(ctx):
    """Start an export from a saved-settings script using the resilient runner."""
    register_process_event(ctx.app)
    adsk.autoTerminate(False)
    return start_export_job(ctx)

def input_value(inputs, name):
    return inputs.itemById(name).value

def input_selected(inputs, name):
    return selected(inputs.itemById(name).listItems)

class ExporterCommandExecuteHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        try:
            inputs = args.command.commandInputs
            iv = partial(input_value, inputs)
            isel = partial(input_selected, inputs)

            save_last_settings({
                I.directory: iv(I.directory),
                I.file_types: isel(I.file_types),
                I.use_active_folder : iv(I.use_active_folder),
                I.show_folders: iv(I.show_folders),
                I.projects: isel(I.projects),
                I.unhide_all: iv(I.unhide_all),
                I.save_sketches: iv(I.save_sketches),
                I.version_count: iv(I.version_count),
                I.all_versions: iv(I.all_versions),
                I.version_separator_is_space: iv(I.version_separator_is_space),
                I.export_non_design_files: iv(I.export_non_design_files),
                I.retry_quarantined: iv(I.retry_quarantined),
                I.minimum_free_memory_gib: iv(I.minimum_free_memory_gib),
            })

            # kinda hacky
            if iv(I.version_separator_is_space):
                global VERSION_SEPARATOR
                VERSION_SEPARATOR = ' '

            ctx = Ctx(
                app = adsk.core.Application.get(),
                folder = Path(iv(I.directory)),
                formats = [FormatFromName[x] for x in isel(I.file_types)],
                use_active_folder = iv(I.use_active_folder),
                projects_folders = make_projects_folders(inputs),
                unhide_all = iv(I.unhide_all),
                save_sketches = iv(I.save_sketches),
                num_versions = -1 if iv(I.all_versions) else iv(I.version_count),
                export_non_design_files = iv(I.export_non_design_files),
                retry_quarantined = iv(I.retry_quarantined),
                minimum_free_memory_gib = iv(I.minimum_free_memory_gib),
            )
            start_export_job(ctx)
        except:
            message_box_traceback()

def run(context):
    ui = None
    try:
        app = adsk.core.Application.get()
        ui = app.userInterface
        cmd_defs = ui.commandDefinitions

        register_process_event(app)

        CMD_DEF_ID = 'aconz2_Exporter'
        cmd_def = cmd_defs.itemById(CMD_DEF_ID)
        # This isn't how all the other demo scripts manage the lifecycle, but if we don't delete the old
        # command then we get double inputs when we run a second time
        if cmd_def:
            cmd_def.deleteMe()

        cmd_def = cmd_defs.addButtonDefinition(
            CMD_DEF_ID,
            'Export all the things',
            'Tooltip',
        )

        cmd_created = ExporterCommandCreatedEventHandler()
        cmd_def.commandCreated.add(cmd_created)
        handlers.append(cmd_created)

        cmd_def.execute()

        adsk.autoTerminate(False)
    except:
        if ui:
            ui.messageBox('Failed:\n{}'.format(traceback.format_exc()))


def stop(context):
    if active_job is not None:
        active_job.request_stop()
    else:
        shutdown_event_pump()
