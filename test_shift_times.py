import unittest
from unittest.mock import patch, Mock

import fitz
import waybill_app as app


class ShiftTimesTests(unittest.TestCase):
    def test_normalization_and_invalid_values(self):
        for value, expected in [('9', '09:00'), (0, '00:00'), ('9:15', '09:15'), ('17:30:00', '17:30'), (None, '')]:
            self.assertEqual(app._normalize_time(value), expected)
        for value in ('25', '09:70', 'abc', '9.5'):
            with self.assertRaises(ValueError):
                app._normalize_time(value)

    def test_all_shifts_sorted_and_assigned_by_date(self):
        records = [dict(Дата=f'2026-07-{day}T00:00:00', Начало='9', Конец=end,
                        КлиентНаименование='Клиент', Количество=quantity)
                   for day, end, quantity in [('20', '17', 8), ('18', '19', 10), ('17', '17', 8)]]
        response = Mock(status_code=200)
        response.json.return_value = records
        with patch.object(app.requests, 'post', return_value=response):
            fields, _ = app.fetch_order_by_pl('test')
        self.assertEqual([fields[f'work_day_{i}'] for i in range(1, 4)], ['17', '18', '20'])
        self.assertEqual([fields[f'time_in_{i}'] for i in range(1, 4)], ['17:00', '19:00', '17:00'])
        self.assertEqual(fields['api_shift_rows'][1]['quantity'], 10)

    def test_existing_day_and_manual_time_preserved(self):
        rows = [{'day': day, 'date': day+'.07.2026', 'object': 'Site', 'start': '09:00', 'end': end}
                for day, end in [('17', '17:00'), ('18', '19:00'), ('20', '17:00')]]
        fields = {'work_day_1': '20', 'time_out_1': '08:30', 'time_in_2': '', 'manual_time_fields': ['time_in_2']}
        app._merge_shift_times(fields, rows)
        self.assertEqual(fields['time_out_1'], '08:30')
        self.assertEqual(fields['work_day_1'], '20')
        self.assertEqual(fields['work_day_2'], '17')
        self.assertEqual(fields['time_in_2'], '')
        self.assertEqual(fields['time_in_3'], '19:00')

    def test_duplicates_not_assigned_to_known_day(self):
        row = {'day': '17', 'object': 'Site', 'start': '09:00', 'end': '17:00'}
        fields = {'work_day_1': '17'}
        app._merge_shift_times(fields, [row, row])
        self.assertNotIn('time_out_1', fields)

    def test_refresh_updates_api_time_but_not_manual(self):
        fields = {'work_day_1': '17', 'time_in_1': '17:00', 'api_time_defaults': {'time_in_1': '17:00'}}
        app._merge_shift_times(fields, [{'day': '17', 'object': '', 'start': '09:00', 'end': '19:00'}])
        self.assertEqual(fields['time_in_1'], '19:00')

    def test_time_picker_and_handwriting_print_protection(self):
        self.assertIn('type="time"', app._field_html('time_out_1', 'Выезд', '09:00'))
        fields = {'time_out_1': '09:00', 'time_in_1': '17:00'}
        filled = dict.fromkeys(app.SCAN_FIELDS, True)
        filled['time_in_1'] = False
        for additions in (False, True):
            data = app.fill_scan_pdf(str(app.TEMPLATE_PATH), fields, filled, additions_only=additions)
            with fitz.open(stream=data, filetype='pdf') as doc:
                self.assertNotIn('09:00', doc[0].get_text())
                self.assertIn('17:00', doc[0].get_text())


if __name__ == '__main__':
    unittest.main()
