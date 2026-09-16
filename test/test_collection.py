import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from finresearch.cninfo import announcements
from finresearch.collection import annotate_listing
from finresearch.storage import Store


class ListingProvider:
    def __init__(self, mode=None): self.mode=mode
    def call(self, role, instructions, prompt, schema, **kwargs):
        items=json.loads(prompt)
        result=[{'url':r['url'],'title':r['title'],'document_type':'PDF','metadata_note':'目录登记'} for r in items]
        if self.mode=='drop': result=result[:-1]
        if self.mode=='invent': result.append({'url':'https://fake.example/file.pdf','title':'fake','document_type':'PDF','metadata_note':''})
        return {'items':result}


class CollectionTest(unittest.TestCase):
    def test_luna_cannot_drop_or_invent_urls(self):
        rows=[{'url':'https://example.org/a.pdf','title':'无关标题'}, {'url':'https://example.org/b.pdf','title':'相关标题'}]
        with tempfile.TemporaryDirectory() as root:
            store=Store(root)
            for mode in ['drop','invent']:
                with self.subTest(mode=mode),self.assertRaisesRegex(ValueError,'遗漏'):
                    annotate_listing(store,rows,'https://example.org',ListingProvider(mode))

    def test_publication_metadata_is_not_replaced_by_luna(self):
        rows=[{'url':'https://example.org/a.pdf','title':'公告','published_at':'2026-09-01T00:00:00+08:00'}]
        with tempfile.TemporaryDirectory() as root:
            result=annotate_listing(Store(root),rows,'https://example.org',ListingProvider())
            self.assertEqual(result['items'][0]['source_metadata']['published_at'],rows[0]['published_at'])

    def test_cninfo_scope_is_bounded_and_dated(self):
        class Response:
            status=200
            def __init__(self): self.data=json.dumps({'totalAnnouncement':20,'announcements':[{'announcementTime':1789056000000,'adjunctUrl':'finalpage/2026-09-11/1225557267.PDF','announcementTitle':'<em>新天</em>公告','secCode':'600956','secName':'新天绿能','announcementId':'1225557267'}]}).encode()
            def getheader(self,key,default=None): return str(len(self.data)) if key=='Content-Length' else default
            def read(self,n): data=self.data[:n];self.data=self.data[n:];return data
        class Connection:
            def __init__(self,*args): pass
            def request(self,*args,**kwargs): pass
            def getresponse(self): return Response()
            def close(self): pass
        with patch('finresearch.cninfo._validate_url',return_value=('https://www.cninfo.com.cn/new/hisAnnouncement/query','www.cninfo.com.cn',443,['1.1.1.1'])),patch('finresearch.cninfo._PinnedHTTPSConnection',Connection):
            result=announcements('经营数据','2026-09-01','2026-09-14',page_size=3,max_pages=1)
        self.assertTrue(result['has_more']);self.assertEqual(result['pages_requested'],1)
        self.assertEqual(result['items'][0]['title'],'新天公告')
        self.assertEqual(result['items'][0]['published_at'],'2026-09-11T00:00:00+08:00')


if __name__=='__main__': unittest.main()
