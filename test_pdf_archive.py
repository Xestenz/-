import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock, AsyncMock

import fitz
from starlette.datastructures import FormData
import pdf_archive as archive
import waybill_app as app


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        check = patch.object(app, '_check_legacy_order_selection', return_value=False)
        check.start()
        self.addCleanup(check.stop)

    def test_single_additions_returns_overlay_but_archives_both_original_pages(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source.pdf'
            with fitz.open() as doc:
                doc.new_page(width=842, height=595).insert_text((30, 30), 'FRONT ORIGINAL')
                doc.new_page(width=842, height=595).insert_text((30, 30), 'REVERSE ORIGINAL')
                doc.save(source)
            entry = {'id': 'single', 'file_path': str(source), 'fields': {},
                     'filled_on_scan': dict.fromkeys(app.SCAN_FIELDS, False)}
            request = Mock()
            request.form = AsyncMock(return_value=FormData({'print_mode': 'additions', 'time_out_1': '09:00'}))
            with patch.object(app, 'waybills', {'single': entry}), patch.object(app, '_save_state'), \
                    patch.object(app, '_auto_calibrate'), patch.object(app, '_save_completed', return_value=[]) as save:
                response = asyncio.run(app.print_waybill('single', request))
            with fitz.open(stream=response.body, filetype='pdf') as doc:
                self.assertEqual(len(doc), 1)
                self.assertNotIn('FRONT ORIGINAL', doc[0].get_text())
                self.assertIn('09:00', doc[0].get_text())
            with fitz.open(stream=save.call_args.args[1], filetype='pdf') as doc:
                self.assertEqual(len(doc), 2)
                self.assertIn('FRONT ORIGINAL', doc[0].get_text())
                self.assertIn('09:00', doc[0].get_text())
                self.assertIn('REVERSE ORIGINAL', doc[1].get_text())

    def test_manual_upload_replaces_matching_inbox_original(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inbox = root / 'inbox'; inbox.mkdir()
            source = inbox / 'scan.pdf'; source.write_bytes(b'original')
            upload = root / 'abcdef12_scan.pdf'; upload.write_bytes(b'original')
            mirror = root / 'mirror'
            entry = {'id': 'manual', 'original_path': str(upload), 'file_path': str(upload)}
            with patch.object(app, 'SCAN_FOLDER', str(inbox)), patch.object(app, 'OUTPUT_MIRROR_FOLDER', str(mirror)), patch.object(app, '_save_state'):
                app._prepare_source(entry)
                paths = app._save_completed(entry, b'completed', 'Client.pdf')
            self.assertFalse(source.exists())
            self.assertEqual(upload.read_bytes(), b'original')
            self.assertEqual(len(paths), 2)
            self.assertTrue(all(Path(p).read_bytes() == b'completed' for p in paths))

    def test_manual_upload_does_not_replace_different_contents(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inbox = root / 'inbox'; inbox.mkdir()
            source = inbox / 'scan.pdf'; source.write_bytes(b'new scan')
            upload = root / 'abcdef12_scan.pdf'; upload.write_bytes(b'old scan')
            entry = {'original_path': str(upload), 'file_path': str(upload)}
            with patch.object(app, 'SCAN_FOLDER', str(inbox)), patch.object(app, '_save_state'):
                app._prepare_source(entry)
            self.assertFalse(entry.get('replace_source', False))
            self.assertEqual(source.read_bytes(), b'new scan')

    def test_additions_batch_archives_full_documents(self):
        entries = {key: {'id': key, 'file_name': key, 'pl_number': number,
            'file_path': str(app.TEMPLATE_PATH), 'fields': {'work_date': '07.10.2026', 'time_out_1': '09:00'},
            'filled_on_scan': dict.fromkeys(app.SCAN_FIELDS, False)}
            for key, number in [('one', '222'), ('two', '111')]}
        request = Mock()
        request.form = AsyncMock(return_value=FormData([('jobs', 'one'), ('jobs', 'two'), ('print_mode', 'additions')]))
        with patch.object(app, 'waybills', entries), patch.object(app, '_save_completed', return_value=[]) as save, patch.object(app, '_save_state'):
            response = asyncio.run(app.batch_print(request))
        with fitz.open(stream=response.body, filetype='pdf') as doc:
            self.assertEqual(len(doc), 2)
            self.assertEqual([row[1] for row in doc.get_toc()], ['111', '222'])
            self.assertIn('09:00', doc[0].get_text())
            self.assertFalse(doc[0].get_images())
        for call in save.call_args_list:
            with fitz.open(stream=call.args[1], filetype='pdf') as archived:
                with fitz.open(app.TEMPLATE_PATH) as original:
                    self.assertEqual(len(archived), len(original))
                    self.assertIn(original[0].get_text().splitlines()[0], archived[0].get_text())
                    if len(original) > 1:
                        self.assertEqual(archived[1].get_text(), original[1].get_text())
                self.assertIn('09:00', archived[0].get_text())

    def test_replace_source_after_both_outputs_and_preserve_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            inbox = Path(directory) / 'inbox'
            inbox.mkdir()
            source = inbox / 'scan.pdf'
            source.write_bytes(b'original')
            cached = archive.preserve_source(source, Path(directory) / 'cache')
            mirror = Path(directory) / 'mirror'
            paths = archive.replace_scanned_file(b'completed', 'Client.pdf', 'one', source, cached, str(mirror))
            self.assertFalse(source.exists())
            self.assertEqual(Path(cached).read_bytes(), b'original')
            self.assertEqual(len(list(inbox.glob('*.pdf'))), 1)
            self.assertEqual(len(paths), 2)
            archive.replace_scanned_file(b'updated', 'Client.pdf', 'one', source, cached, str(mirror))
            self.assertEqual(len(list(inbox.glob('*.pdf'))), 1)

    def test_replace_failure_leaves_original(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'scan.pdf'
            source.write_bytes(b'original')
            cached = archive.preserve_source(source, Path(directory) / 'cache')
            blocked = Path(directory) / 'blocked'
            blocked.write_text('not a folder')
            with self.assertRaises(OSError):
                archive.replace_scanned_file(b'completed', 'Client.pdf', 'one', source, cached, str(blocked))
            self.assertEqual(source.read_bytes(), b'original')
            source.write_bytes(b'changed')
            with self.assertRaises(ValueError):
                archive.replace_scanned_file(b'completed', 'Client.pdf', 'one', source, cached)
            self.assertEqual(source.read_bytes(), b'changed')

    def test_working_source_survives_original_move(self):
        with tempfile.TemporaryDirectory() as directory:
            original = Path(directory) / 'scan.pdf'
            original.write_bytes(b'original scan')
            cached = Path(archive.preserve_source(original, Path(directory) / 'private'))
            original.rename(Path(directory) / 'renamed.pdf')
            self.assertEqual(cached.read_bytes(), b'original scan')

    def test_group_ignores_review_flags_and_excludes_other_groups(self):
        request = Mock()
        request.form = AsyncMock(return_value=FormData([('group_job', 'one')]))
        entries = {key: {'id': key, 'file_name': key, 'file_path': str(app.TEMPLATE_PATH),
            'batch_id': group, 'review_fields': ['time_in_1'],
            'fields': {'work_date': '07.10.2026'},
            'filled_on_scan': dict.fromkeys(app.SCAN_FIELDS, True)}
            for key, group in [('one', 'A'), ('two', 'A'), ('other', 'B')]}
        with patch.object(app, 'waybills', entries), patch.object(app, 'archive_pdf', return_value=[]) as save, \
             patch.object(app, '_save_state'), patch.object(app, '_queued_batches', {}):
            response = asyncio.run(app.batch_print(request))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(save.call_count, 2)
        with fitz.open(stream=response.body, filetype='pdf') as doc:
            self.assertEqual(len(doc), 2)

    def test_failed_mirror_keeps_primary_and_retry_reuses_file(self):
        with tempfile.TemporaryDirectory() as directory:
            primary = Path(directory) / 'primary'
            blocked = Path(directory) / 'blocked'
            blocked.write_text('not a directory')
            with self.assertRaises(OSError):
                archive.archive_pdf(b'pdf', 'Client.pdf', 'abc', str(primary), str(blocked))
            self.assertEqual(len(list(primary.glob('*.pdf'))), 1)
            archive.archive_pdf(b'pdf', 'Client.pdf', 'abc', str(primary), str(Path(directory) / 'mirror'))
            self.assertEqual(len(list(primary.glob('*.pdf'))), 1)

    def test_repeat_and_same_directory_and_generated_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            with fitz.open() as doc:
                doc.new_page()
                data = archive.marked_pdf(doc.tobytes())
            first = archive.archive_pdf(data, 'Client.pdf', 'abc', directory, directory)
            self.assertEqual(len(first), 1)
            self.assertTrue(archive.is_completed(first[0]))
            self.assertEqual(first, archive.archive_pdf(data, 'Client.pdf', 'abc', directory))
            archive.archive_pdf(data, 'Client.pdf', 'def', directory)
            self.assertEqual(len(list(Path(directory).glob('*.pdf'))), 2)

    def test_batch_saves_individuals_and_combines_pages(self):
        with tempfile.TemporaryDirectory() as directory:
            entries = {key: {'id': key, 'file_name': key, 'file_path': str(app.TEMPLATE_PATH),
                'fields': {'work_date': '07.10.2026'}, 'filled_on_scan': dict.fromkeys(app.SCAN_FIELDS, False)}
                for key in ('one', 'two')}
            request = Mock()
            request.form = AsyncMock(return_value=FormData([('jobs', 'one'), ('jobs', 'two'), ('print_mode', 'copy')]))
            with patch.object(app, 'waybills', entries), patch.object(app, 'OUTPUT_FOLDER', directory), \
                 patch.object(app, 'OUTPUT_MIRROR_FOLDER', ''), patch.object(app, '_save_state'):
                result = asyncio.run(app.batch_print(request))
            with fitz.open(stream=result.body, filetype='pdf') as doc:
                self.assertEqual(len(doc), 4)
            self.assertEqual(len(list(Path(directory).glob('*.pdf'))), 2)

    def test_duplicate_input_skips_analysis(self):
        import hashlib
        path = str(app.TEMPLATE_PATH)
        entries = {'one': {'id': 'one', 'file_path': path, 'source_hash': hashlib.sha256(Path(path).read_bytes()).hexdigest()}}
        with patch.object(app, 'waybills', entries), patch.object(app, '_handle_unique_scan') as process, \
             patch.object(app.webbrowser, 'open'):
            app._handle_new_scan(path)
        process.assert_not_called()


if __name__ == '__main__':
    unittest.main()
