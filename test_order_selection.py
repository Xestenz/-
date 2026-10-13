import asyncio
import unittest
from unittest.mock import patch, Mock, AsyncMock
from fastapi import HTTPException
import waybill_app as app


def records():
    return [dict(Дата='2026-10-01T00:00:00', НомерЗаказа='000009127',
                 КлиентНаименование='ЭНЕРГОСТРОЙ', МашинаНаименование='JCB СМ26'),
            dict(Дата='2026-10-05T00:00:00', НомерЗаказа='000009183',
                 КлиентНаименование='КОРСТРОЙ', МашинаНаименование='JCB СМ08')]


def date(value, confident=True):
    return {'date': value, 'confidence': {'date': confident}}


class OrderSelectionTests(unittest.TestCase):
    def test_can_switch_back_after_automatic_selection(self):
        fields, _ = app.fetch_order_by_pl('test', records_override=records())
        w = {'id': 'test', 'fields': fields, 'reverse_times': [date('05.10')], 'pl_number': 'test'}
        app._resolve_order_candidates(w)
        self.assertEqual(len(w['fields']['api_records']), 2)
        request = Mock()
        request.form = AsyncMock(return_value={'order_index': '0'})
        with patch.object(app, 'waybills', {'test': w}), patch.object(app, '_save_state'), patch.object(app, '_apply_reverse_times'):
            asyncio.run(app.select_order('test', request))
            self.assertEqual(w['fields']['order_number'], '000009127')
            request.form = AsyncMock(return_value={'order_index': '1'})
            asyncio.run(app.select_order('test', request))
        self.assertEqual(w['fields']['order_number'], '000009183')
        self.assertEqual(len(w['fields']['api_records']), 2)

    def test_same_order_multiple_days_can_be_selected_separately(self):
        rows = [records()[1], {**records()[1], 'Дата': '2026-10-06T00:00:00'}]
        options = app._selection_options(rows)
        self.assertEqual(options, [rows, [rows[0]], [rows[1]]])

    def test_legacy_wrong_order_requires_review_before_print(self):
        fresh, warning = app.fetch_order_by_pl('test', records_override=records())
        w = {'pl_number': 'test', 'fields': {'customer': 'Old wrong customer'}}
        with patch.object(app, 'fetch_order_by_pl', return_value=(fresh, warning)), patch.object(app, '_save_state'):
            self.assertTrue(app._check_legacy_order_selection(w))
        self.assertTrue(w['fields']['order_candidates'])
        self.assertFalse(w['fields']['customer'])

    def test_mixed_barcode_does_not_choose_earliest_order(self):
        fields, warning = app.fetch_order_by_pl('test', records_override=records())
        self.assertFalse(fields['customer'])
        self.assertFalse(fields['vehicle_type'])
        self.assertEqual(len(fields['order_candidates']), 2)
        entry = {'fields': fields, 'reverse_times': [date('05.10')], 'pl_number': 'test'}
        app._resolve_order_candidates(entry)
        self.assertEqual(entry['fields']['order_number'], '000009183')
        self.assertEqual(entry['fields']['vehicle_type'], 'JCB СМ08')
        self.assertEqual(len(entry['fields']['api_shift_rows']), 1)
        self.assertNotIn('order_candidates', entry['fields'])

    def test_uncertain_conflicting_or_missing_dates_require_choice(self):
        groups = app._order_groups(records())
        for rows in ([], [date('05.10', False)], [date('bad')],
                     [date('01.10'), date('05.10')], [date('05.10.2025')]):
            self.assertIsNone(app._match_scan_dates(groups, rows))
        same_day = records()
        same_day[0]['Дата'] = same_day[1]['Дата']
        self.assertIsNone(app._match_scan_dates(app._order_groups(same_day), [date('05.10')]))

    def test_pending_selection_blocks_print_and_allows_manual_choice(self):
        fields, _ = app.fetch_order_by_pl('test', records_override=records())
        entry = {'id': 'test', 'fields': fields, 'pl_number': 'test'}
        request = Mock()
        request.form = AsyncMock(return_value={'order_index': '1'})
        with patch.object(app, 'waybills', {'test': entry}), patch.object(app, '_save_state'), patch.object(app, '_apply_reverse_times'):
            with self.assertRaises(HTTPException):
                asyncio.run(app.print_waybill('test', request))
            result = asyncio.run(app.select_order('test', request))
        self.assertEqual(result.status_code, 303)
        self.assertEqual(entry['fields']['order_number'], '000009183')
        self.assertTrue(entry['order_selection_confirmed'])


if __name__ == '__main__':
    unittest.main()
