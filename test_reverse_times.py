import unittest
import json
from unittest.mock import Mock, patch

import waybill_app as app


class ReverseTimesTests(unittest.TestCase):
    def test_reconstruct_and_validate_time(self):
        row = {'date': '18.07', 'date_confident': True, 'start': '09:00',
               'start_confident': True, 'end': '17:02', 'end_confident': False,
               'hours_note': '7+1', 'hours_confident': True}
        resolved = app._resolve_reverse_row(row)
        self.assertEqual(resolved['end'], '17:00')
        self.assertTrue(resolved['confident'])
        backward = app._resolve_reverse_row({**row, 'start': '', 'start_confident': False,
                                             'end': '17:30', 'end_confident': True})
        self.assertEqual(backward['start'], '09:30')
        self.assertTrue(backward['confident'])
        conflict = app._resolve_reverse_row({**row, 'end': '18:00', 'end_confident': True})
        self.assertFalse(conflict['confident'])
        self.assertEqual(conflict['end'], '18:00')
        uncertain = app._resolve_reverse_row({**row, 'hours_confident': False})
        self.assertFalse(uncertain['confident'])
        self.assertEqual(uncertain['end'], '')
        self.assertFalse(app._resolve_reverse_row({**row, 'date_confident': False})['confident'])

    def test_confident_reverse_overrides_api_but_not_manual_or_handwriting(self):
        w = {'fields': {'work_day_1': '24', 'api_shift_rows': [{'day': '24', 'date': '24.09.2026'}],
                        'time_in_1': '16:00'}, 'reverse_times': [
            {'date': '24.09', 'start': '09:00', 'end': '17:00', 'confident': True}]}
        app._apply_reverse_times(w)
        self.assertEqual(w['fields']['time_in_1'], '17:00')
        w['fields'].update(time_in_1='18:00', manual_time_fields=['time_in_1'])
        app._apply_reverse_times(w)
        self.assertEqual(w['fields']['time_in_1'], '18:00')
        w['fields'].update(time_in_1='16:00', manual_time_fields=[])
        w['reverse_times'][0]['confident'] = False
        app._apply_reverse_times(w)
        self.assertEqual(w['fields']['time_in_1'], '16:00')

    def test_matches_date_not_position_and_rejects_ambiguity(self):
        fields = {'work_day_1': '24', 'api_shift_rows': [{'day': '24', 'date': '24.09.2026'}]}
        row = {'date': '24.09', 'start': '09:00', 'end': '17:00', 'confident': True}
        self.assertEqual(app._reverse_match(fields, [row], 1), row)
        self.assertIsNone(app._reverse_match(fields, [row, row], 1))
        self.assertIsNone(app._reverse_match(fields, [{**row, 'date': '24.07'}], 1))
        self.assertIsNone(app._reverse_match(fields, [{**row, 'confident': False}], 1))

    def test_hours_note_never_used_to_calculate_missing_end(self):
        response = Mock()
        response.json.return_value = {'choices': [{'message': {'content': json.dumps({'rows': [
            {'date': '24.09', 'start': '9', 'end': '', 'hours_note': '7+1', 'confident': True},
            {'date': '25.09', 'start': '9', 'end': '17', 'hours_note': '7+1', 'confident': True}
        ]})}}]}
        with patch.object(app.requests, 'post', return_value=response):
            rows = app.read_reverse_times(str(app.TEMPLATE_PATH))
        self.assertEqual(rows[0]['end'], '')
        self.assertFalse(rows[0]['confident'])
        self.assertEqual(rows[1]['end'], '17:00')
        self.assertEqual(rows[1]['hours_note'], '7+1')


if __name__ == '__main__':
    unittest.main()
