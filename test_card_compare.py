import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import httpx
from app.services.card_compare import CardCompare, CardCompareError, normalize_card, parse_nm_ids
import app.services.card_compare as card_compare_module
from app.storage import DraftStore

class CardCompareTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = DraftStore(f"sqlite:///{Path(self.tmp.name) / 'db.sqlite3'}")
        self.cards = {101: {"nmID":101,"title":"A","vendorCode":"A-1","brand":"B","subjectName":"Jeans","description":"desc","dimensions":{"length":10},"characteristics":[{"name":"Цвет","value":"синий"},{"name":"Состав","value":["хлопок","эластан"]}],"sizes":[{"techSize":"32","wbSize":"48"}],"photos":[{"big":"https://x/1.jpg"}]}, 202: {"nmID":202,"title":"C","vendorCode":"C-2","brand":"B2","characteristics":[{"name":"Цвет","value":"черный"}],"sizes":[{"techSize":"34","wbSize":""}],"photos":[]}}
        transport = httpx.MockTransport(self._request)
        real_client = httpx.Client
        self.patch = mock.patch.object(httpx, 'Client', lambda **kw: real_client(transport=transport, **kw))
        self.patch.start(); self.addCleanup(self.patch.stop)
        self.service = CardCompare(self.store, lambda:[('WB','token')])
    def _request(self, request):
        if getattr(self, 'retry_mode', False) and not getattr(self, 'retry_seen', False):
            self.retry_seen = True
            return httpx.Response(429, headers={'Retry-After': '0'}, json={})
        body = json.loads(request.content)
        text = str(body.get('settings',{}).get('filter',{}).get('textSearch'))
        cards = [self.cards[int(text)]] if text.isdigit() and int(text) in self.cards else []
        return httpx.Response(200, json={'cards':cards})
    def test_normalize_union_and_sizes(self):
        d = self.service.create('grp',[101,202])
        self.assertEqual([r['name'] for r in d['rows'] if r['name']=='Цвет'], ['Цвет'])
        row = next(r for r in d['rows'] if r['name']=='Состав')
        self.assertEqual(row['values']['202'], '')
        self.assertEqual(d['cards'][0]['sizes'][0]['techSize'],'32')
        self.assertEqual(d['cards'][0]['sizes'][0]['wbSize'],'48')
        self.assertEqual(d['cards'][1]['sizes'][0]['wbSize'],'')
    def test_persistence_and_invalid(self):
        self.service.create('grp',[101])
        self.assertEqual(len(self.service.list()),1)
        self.assertEqual(parse_nm_ids('101 202, 303'), [101, 202, 303])
        with self.assertRaises(ValueError): parse_nm_ids('abc')
        with self.assertRaises(CardCompareError): self.service.detail(999)

    def test_rate_limit_retries_then_keeps_card(self):
        self.retry_mode = True
        with mock.patch.object(card_compare_module.time, 'sleep') as pause:
            result = self.service.create('retry', [101])
        self.assertEqual(result['cards'][0]['nm_id'], 101)
        self.assertFalse(result['cards'][0].get('error'))
        pause.assert_called_once()

if __name__ == '__main__': unittest.main()




