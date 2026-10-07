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
    def test_batch_review_error_is_readable_and_links_to_document(self):
        request = Mock()
        request.form = AsyncMock(return_value=FormData([('jobs', 'one')]))
        entry = {'file_name': 'scan.pdf', 'review_fields': ['time_in_1'],
                 'filled_on_scan': dict.fromkeys(app.SCAN_FIELDS, True)}
        with patch.object(app, 'waybills', {'one': entry}), patch.object(app, 'archive_pdf') as save:
            response = asyncio.run(app.batch_print(request))
        self.assertEqual(response.status_code, 400)
        self.assertIn('/waybill/one', response.body.decode())
        self.assertIn('Возвращение', response.body.decode())
        save.assert_not_called()

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
            request.form = AsyncMock(return_value=FormData([('jobs', 'one'), ('jobs', 'two')]))
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
