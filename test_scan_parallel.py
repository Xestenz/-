import threading
import unittest
from unittest.mock import patch

import waybill_app as app


class ParallelScanTests(unittest.TestCase):
    def test_all_three_stages_can_run_together(self):
        barrier = threading.Barrier(3, timeout=5)
        def front(*args):
            barrier.wait()
            return dict.fromkeys(app.SCAN_FIELDS, False)
        def reverse(*args):
            barrier.wait()
            return []
        def barcode(*args):
            barrier.wait()
            return None
        with patch.object(app, 'waybills', {}), patch.object(app, '_save_state'), \
             patch.object(app.webbrowser, 'open'), patch.object(app, 'detect_fields_with_ai', side_effect=front), \
             patch.object(app, 'read_reverse_times', side_effect=reverse), patch.object(app, 'read_barcode', side_effect=barcode):
            entry = app._handle_unique_scan('test.pdf', 'test-hash')
        self.assertFalse(entry['processing'])
        self.assertIsNone(entry['detection_warning'])
        self.assertIsNone(entry['error'])
        self.assertEqual(set(entry['stage_seconds']), {'front', 'reverse', 'order'})

    def test_queue_suppresses_same_path_while_running(self):
        entered = threading.Event()
        release = threading.Event()
        def work(path):
            entered.set()
            release.wait(5)
        with patch.object(app, '_handle_new_scan', side_effect=work) as process:
            future = app._enqueue_scan('parallel-test.pdf')
            try:
                self.assertTrue(entered.wait(5))
                self.assertIsNone(app._enqueue_scan('parallel-test.pdf'))
            finally:
                release.set()
                future.result(timeout=5)
            self.assertEqual(process.call_count, 1)


if __name__ == '__main__':
    unittest.main()
