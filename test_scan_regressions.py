import json
import unittest
from unittest.mock import patch
import fitz

import waybill_app as app


class ScanRegressions(unittest.TestCase):
    def test_provider_preamble_and_truncation(self):
        fields = dict.fromkeys(app.SCAN_FIELDS, False)
        response = {'choices': [{'finish_reason': 'stop', 'message': {
            'content': 'Explanation before JSON\n```json\n' + json.dumps(fields) + '\n```'}}]}
        self.assertEqual(app._parse_field_response(response), fields)
        response['choices'][0]['finish_reason'] = 'length'
        with self.assertRaises(ValueError):
            app._parse_field_response(response)
        response['choices'][0]['finish_reason'] = 'stop'
        response['choices'][0]['message']['content'] = '{"customer": false}'
        with self.assertRaises(ValueError):
            app._parse_field_response(response)

    def test_machine_and_date_range_filename(self):
        fields = {'customer_short_name': 'АССИСТЭНЦИЯ ООО ПФ',
                  'vehicle_type': 'Экскаватор погрузчик JCB СМ41',
                  'api_shift_rows': [{'date': '26.09.2026'}, {'date': '27.09.2026'}]}
        entry = {'id': 'test', 'fields': fields}
        self.assertEqual(app._default_pdf_name(entry), 'АССИСТЭНЦИЯ ООО ПФ 41 ед 26-27,09')
        fields['output_filename'] = 'ВЭС ООО 67 ед 22-24,09'
        self.assertEqual(app._default_pdf_name(entry), fields['output_filename'])

    def test_retry_recovers_failed_analysis(self):
        entry = {'id': 'test', 'file_path': 'scan.pdf', 'fields': {},
                 'detection_warning': 'failed'}
        detected = dict.fromkeys(app.SCAN_FIELDS, False)
        with patch.object(app, 'waybills', {'test': entry}), \
                patch.object(app, 'detect_fields_with_ai', return_value=detected), \
                patch.object(app, '_apply_reverse_times'), patch.object(app, '_save_state'):
            self.assertEqual(app.retry_analysis('test'), {'ok': True})
        self.assertNotIn('detection_warning', entry)
        self.assertFalse(entry['processing'])
        self.assertEqual(entry['filled_on_scan'], detected)

    def test_customer_prints_two_lines_inside_band(self):
        customer = 'ООО Тест, ИНН 1234567890, КПП 123456789, город Москва, улица Примерная, дом 123, телефон 1234567'
        data = app.fill_scan_pdf(str(app.TEMPLATE_PATH), {'customer': customer},
                                 dict.fromkeys(app.SCAN_FIELDS, False), additions_only=True)
        with fitz.open(stream=data, filetype='pdf') as doc:
            lines = [line for block in doc[0].get_text('dict')['blocks']
                     for line in block.get('lines', [])]
            self.assertEqual(len(lines), 2)
            for line in lines:
                for span in line['spans']:
                    self.assertGreaterEqual(span['size'], 6)


if __name__ == '__main__':
    unittest.main()
