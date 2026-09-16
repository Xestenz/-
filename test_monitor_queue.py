"""Проверки очереди без запросов и записи в 1С: python -m unittest test_monitor_queue."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import monitor_pl as monitor


class MonitorQueueTests(unittest.TestCase):
    def test_failures_do_not_block_older_shifts_and_progress_is_saved(self):
        orders = [dict(id=str(i), num=str(i), namef=f'{i}.png') for i in range(5)]
        seen = []
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(monitor, 'QUEUE_STATE_FILE', Path(folder) / 'queue.json'), \
                patch.object(monitor, 'MAX_ORDERS_PER_RUN', 2), \
                patch.object(monitor, 'get_orders_without_pl', return_value=orders), \
                patch.object(monitor, 'process_order', side_effect=lambda o: seen.append(o['id']) or 'no_barcode'):
            monitor.run_once()
            self.assertEqual(seen, ['4', '3'])
            self.assertEqual(len(monitor._load_queue_state()), 2)
            monitor.run_once()
            self.assertEqual(seen, ['4', '3', '2', '1'])
            monitor.run_once()
            self.assertEqual(seen[:5], ['4', '3', '2', '1', '0'])

    def test_shift_ids_and_changed_files_are_distinct(self):
        a = dict(id='a', num='100', namef='old.png')
        b = dict(id='b', num='100', namef='old.png')
        changed = dict(a, namef='new.png')
        state = {monitor._queue_key(a): 10}
        batch = monitor._select_batch([a, a, b, changed], state, 10)
        self.assertEqual(batch, [b, changed, a])

    def test_dry_run_is_not_counted_as_written(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'scan.png'
            path.touch()
            order = dict(id='test', num='test', namef=str(path))
            with patch.object(monitor, 'read_barcode_from_file', return_value=['2026000057859']), \
                    patch.object(monitor, 'write_pl_to_order', return_value=True):
                with patch.object(monitor, 'DRY_RUN', True):
                    self.assertEqual(monitor.process_order(order), 'dry_run')
                with patch.object(monitor, 'DRY_RUN', False):
                    self.assertEqual(monitor.process_order(order), 'written')


if __name__ == '__main__':
    unittest.main()
