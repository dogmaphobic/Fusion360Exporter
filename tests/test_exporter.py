import importlib.util
import os
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


def install_adsk_stub():
    adsk = types.ModuleType('adsk')
    core = types.ModuleType('adsk.core')
    drawing = types.ModuleType('adsk.drawing')
    fusion = types.ModuleType('adsk.fusion')

    for name in (
        'CustomEventHandler',
        'InputChangedEventHandler',
        'CommandCreatedEventHandler',
        'CommandEventHandler',
    ):
        setattr(core, name, type(name, (), {}))
    for name in ('Application', 'DataFile', 'Document'):
        setattr(core, name, type(name, (), {}))
    drawing.DrawingExportManager = type('DrawingExportManager', (), {})
    drawing.Drawing = type('Drawing', (), {'cast': staticmethod(lambda value: value)})
    fusion.FusionDocument = type(
        'FusionDocument', (), {'cast': staticmethod(lambda value: value)}
    )
    core.DropDownStyles = types.SimpleNamespace(CheckBoxDropDownStyle=object())

    adsk.core = core
    adsk.drawing = drawing
    adsk.fusion = fusion
    adsk.autoTerminate = Mock()
    adsk.terminate = Mock()
    sys.modules['adsk'] = adsk
    sys.modules['adsk.core'] = core
    sys.modules['adsk.drawing'] = drawing
    sys.modules['adsk.fusion'] = fusion


install_adsk_stub()

import Exporter
from resilience import GIB, MemorySnapshot, partial_output_path


class ImportBootstrapTests(unittest.TestCase):
    def test_entry_point_finds_adjacent_module_when_fusion_omits_script_path(self):
        repository = Path(Exporter.__file__).resolve().parent
        module_name = 'fusion_loader_import_test'
        original_path = list(sys.path)
        original_resilience = sys.modules.pop('resilience', None)
        try:
            sys.path[:] = [
                entry
                for entry in sys.path
                if Path(entry or os.getcwd()).resolve() != repository
            ]
            spec = importlib.util.spec_from_file_location(module_name, repository / 'Exporter.py')
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
            self.assertEqual(repository, module.SCRIPT_DIRECTORY)
            self.assertIn(str(repository), sys.path)
        finally:
            sys.modules.pop(module_name, None)
            sys.modules.pop('resilience', None)
            if original_resilience is not None:
                sys.modules['resilience'] = original_resilience
            sys.path[:] = original_path


class AtomicOutputTests(unittest.TestCase):
    def test_success_replaces_target_and_sets_cloud_time(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'model.step'
            target.write_bytes(b'old')

            def write(path):
                path.write_bytes(b'new STEP data')
                return True

            Exporter.atomic_output(target, 123, write)

            self.assertEqual(b'new STEP data', target.read_bytes())
            self.assertEqual(123, int(target.stat().st_mtime))
            self.assertFalse(partial_output_path(target).exists())

    def test_failure_preserves_existing_target_and_removes_partial(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'model.f3d'
            target.write_bytes(b'known good archive')

            def write(path):
                path.write_bytes(b'broken replacement')
                return False

            with self.assertRaises(RuntimeError):
                Exporter.atomic_output(target, 123, write)

            self.assertEqual(b'known good archive', target.read_bytes())
            self.assertFalse(partial_output_path(target).exists())


class ContextCompatibilityTests(unittest.TestCase):
    def test_old_saved_script_accepts_uppercase_formats_and_new_defaults(self):
        ctx = Exporter.Ctx.from_dict(
            {
                'folder': '/tmp/export',
                'formats': ['F3D', 'STEP'],
                'projects_folders': {'project': []},
                'unhide_all': True,
                'save_sketches': False,
                'num_versions': 0,
            },
            app=object(),
        )
        self.assertEqual([Exporter.Format.F3D, Exporter.Format.STEP], ctx.formats)
        self.assertFalse(ctx.use_active_folder)
        self.assertFalse(ctx.retry_quarantined)
        self.assertEqual(4, ctx.minimum_free_memory_gib)


class FailedOpenTests(unittest.TestCase):
    def make_ctx(self, app, directory):
        return Exporter.Ctx(
            app=app,
            folder=Path(directory),
            formats=[Exporter.Format.F3D, Exporter.Format.STEP],
            projects_folders={},
            use_active_folder=False,
            unhide_all=False,
            save_sketches=False,
            num_versions=0,
            export_non_design_files=False,
        )

    def test_open_exception_is_counted_once_and_model_is_skipped(self):
        with tempfile.TemporaryDirectory() as directory:
            documents = Mock()
            documents.open.side_effect = RuntimeError('cloud model unavailable')
            app = types.SimpleNamespace(documents=documents, activeDocument=None)
            file = types.SimpleNamespace(
                name='Trooper Payload Tray',
                versionNumber=45,
                versionId='version-45',
                id='model',
                fileExtension='f3d',
                dateModified=100,
            )

            with patch.object(Exporter, 'log'), patch.object(
                Exporter, 'output_path_exists', return_value=False
            ):
                result = Exporter.visit_file(self.make_ctx(app, directory), file)

            self.assertEqual(1, result.errored)
            self.assertEqual(1, documents.open.call_count)

    def test_failed_close_becomes_a_controlled_stop(self):
        document = Mock()
        document.activate.return_value = True
        document.close.return_value = False
        app = types.SimpleNamespace(
            documents=types.SimpleNamespace(open=Mock(return_value=document)),
            activeDocument=None,
        )
        file = types.SimpleNamespace(
            name='Model', versionNumber=1, versionId='version-1'
        )
        with patch.object(Exporter, 'log'):
            lazy = Exporter.LazyDocument(self.make_ctx(app, '/tmp'), file)
            lazy.open()
            with self.assertRaises(Exporter.DocumentCloseError):
                lazy.close()


class RunnerPolicyTests(unittest.TestCase):
    def test_low_memory_stops_before_next_model(self):
        job = object.__new__(Exporter.ExportJob)
        job.prepared = True
        job.items = [object()]
        job.index = 0
        job.stop_requested = False
        job.stop_reason = None
        job.progress = types.SimpleNamespace(wasCancelled=False)
        job.counter = Exporter.Counter()
        job.last_memory = None
        job.memory_monitor = Mock()
        snapshot = MemorySnapshot(2 * GIB, 3 * GIB, 64 * GIB)
        job.memory_monitor.sample.return_value = snapshot
        job.memory_monitor.is_low.return_value = True
        job.memory_monitor.safety_floor.return_value = int(6.4 * GIB)

        with patch.object(Exporter, 'log'):
            self.assertFalse(job.step())

        self.assertEqual('memory', job.stop_reason)
        self.assertEqual(0, job.index)

    def test_close_failure_keeps_journal_record_for_next_run_quarantine(self):
        job = object.__new__(Exporter.ExportJob)
        job.ctx = types.SimpleNamespace(retry_quarantined=False)
        job.counter = Exporter.Counter()
        job.stop_reason = None
        job.control = object()
        job.journal = Mock()
        job.journal.is_quarantined.return_value = False
        item = types.SimpleNamespace(
            key='version-45',
            file=types.SimpleNamespace(name='Ancient model', versionNumber=45),
            ctx=object(),
            journal_record=Mock(return_value={'key': 'version-45'}),
        )

        with patch.object(
            Exporter, 'visit_file', side_effect=Exporter.DocumentCloseError('still open')
        ), patch.object(Exporter, 'log'):
            job.process_item(item)

        job.journal.begin.assert_called_once_with({'key': 'version-45'})
        job.journal.finish.assert_not_called()
        self.assertEqual('close failure', job.stop_reason)


class CustomEventPumpTests(unittest.TestCase):
    def test_worker_fires_exactly_once_when_fusion_reports_false(self):
        first_call = threading.Event()
        duplicate_call = threading.Event()
        calls = 0

        def fire_custom_event(event_id, additional_info):
            nonlocal calls
            calls += 1
            if calls == 1:
                first_call.set()
            else:
                duplicate_call.set()
            return False

        app = types.SimpleNamespace(fireCustomEvent=fire_custom_event)
        pump = Exporter.CustomEventPump(app, dispatch_delay=0)
        pump.start()
        try:
            pump.request()
            self.assertTrue(first_call.wait(1), 'event pump did not dispatch from its worker')
            self.assertFalse(duplicate_call.wait(0.05), 'event pump retried a false result')
        finally:
            pump.shutdown()
            pump.join(1)
        self.assertFalse(pump.is_alive())

    def test_handler_ignores_reentrant_callback(self):
        job = types.SimpleNamespace(step_active=True)
        original_job = Exporter.active_job
        Exporter.active_job = job
        try:
            with patch.object(Exporter, 'log') as logger:
                Exporter.ExporterProcessEventHandler().notify(object())
            logger.assert_called_once_with('WARNING: ignored a re-entrant export event')
        finally:
            Exporter.active_job = original_job


if __name__ == '__main__':
    unittest.main()
