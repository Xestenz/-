import asyncio
import unittest
from unittest.mock import Mock, patch

import waybill_app as app


def response(data):
    result = Mock(status_code=200)
    result.json.return_value = data
    return result


class CustomerRequisitesTests(unittest.TestCase):
    def setUp(self):
        config = patch.object(app, 'ORG_REQUISITES', '')
        config.start()
        self.addCleanup(config.stop)

    def test_open_old_waybill_refreshes_customer_and_uses_local_company(self):
        old = {'id': 'test', 'pl_number': 'test', 'fields': {'customer': 'Краткое имя'}}
        fresh = {**app._empty_fields(), 'customer': 'Клиент ИНН 123 КПП 456',
                 'customer_code': 'A', 'customer_details_loaded': True}
        with patch.object(app, 'ORG_REQUISITES', 'Своя организация ИНН 789'), \
                patch.object(app, 'waybills', {'test': old}), \
                patch.object(app, 'fetch_order_by_pl', return_value=(fresh, None)) as fetch, \
                patch.object(app, '_save_state'), \
                patch.object(app, '_render_waybill', return_value='ok'):
            asyncio.run(app.waybill_page('test'))
            asyncio.run(app.waybill_page('test'))
        self.assertEqual(fetch.call_count, 1)
        self.assertIn('ИНН 123', old['fields']['customer'])
        self.assertIn('ИНН 789', old['fields']['company_name'])

    def test_order_uses_details_by_exact_code_and_keeps_short_filename(self):
        order = {'КлиентКод': 'БУ-010986', 'КлиентНаименование': 'КЛИЕНТ ООО', 'Дата': '2026-09-04'}
        details = [{'code': 'БУ-010986', 'ПредставлениеПокупателя': 'ООО КЛИЕНТ, ИНН 123, КПП 456, адрес'}]
        with patch.object(app.requests, 'post', side_effect=[response([order]), response(details)]) as post:
            fields, warning = app.fetch_order_by_pl('2026000057859')
        self.assertTrue(fields['customer_details_loaded'])
        self.assertIn('ИНН 123', fields['customer'])
        self.assertEqual(fields['company_name'], '')
        self.assertEqual(post.call_args.kwargs['params'], {'code': 'БУ-010986'})
        self.assertEqual(app._default_pdf_name({'id': 'test', 'fields': fields}), 'КЛИЕНТ ООО 04,09')
        self.assertIn('организац', warning)

    def test_wrong_customer_is_not_used(self):
        order = {'КлиентКод': 'A', 'КлиентНаименование': 'Правильный клиент'}
        with patch.object(app.requests, 'post', side_effect=[response([order]), response([
            {'code': 'B', 'ПредставлениеПокупателя': 'Чужие реквизиты'}
        ])]):
            fields, warning = app.fetch_order_by_pl('test')
        self.assertEqual(fields['customer'], 'Правильный клиент')
        self.assertFalse(fields['customer_details_loaded'])
        self.assertIn('не получены', warning)

    def test_missing_code_does_not_lookup_by_name(self):
        with patch.object(app.requests, 'post', return_value=response([{'КлиентНаименование': 'Клиент'}])) as post:
            fields, warning = app.fetch_order_by_pl('test')
        self.assertEqual(post.call_count, 1)
        self.assertFalse(fields['customer_details_loaded'])
        self.assertIn('код клиента', warning)

    def test_refresh_preserves_manual_company_and_other_fields(self):
        old = {'pl_number': 'test', 'fields': {'company_name': 'Моя организация',
               'company_name_manual': True, 'driver_name': 'Мой водитель'}}
        fresh = {**app._empty_fields(), 'customer': 'Реквизиты', 'customer_code': 'A'}
        with patch.object(app, 'waybills', {'test': old}), \
                patch.object(app, 'fetch_order_by_pl', return_value=(fresh, 'warning')), \
                patch.object(app, '_save_state'):
            asyncio.run(app.refresh_requisites('test'))
        self.assertEqual(old['fields']['company_name'], 'Моя организация')
        self.assertEqual(old['fields']['driver_name'], 'Мой водитель')
        self.assertEqual(old['fields']['customer'], 'Реквизиты')


if __name__ == '__main__':
    unittest.main()
