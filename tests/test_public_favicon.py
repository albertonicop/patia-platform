"""The public homepage advertises a crawlable official square PNG favicon."""
from html.parser import HTMLParser
import importlib.util
from pathlib import Path
import struct
import unittest

import test_sales_ticket_foundation as fixtures


class HeadParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_head = False
        self.icons = []
        self.robots = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'head':
            self.in_head = True
        if self.in_head and tag == 'link' and 'icon' in attrs.get('rel', ''):
            self.icons.append(attrs)
        if self.in_head and tag == 'meta' and attrs.get('name') == 'robots':
            self.robots.append(attrs.get('content', '').lower())

    def handle_endtag(self, tag):
        if tag == 'head':
            self.in_head = False


class PublicFaviconTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SalesTicketFoundationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.client = self.fixture.app.test_client()

    def test_anonymous_google_crawlers_can_fetch_home_and_single_favicon(self):
        for agent in ('Googlebot', 'Googlebot-Image'):
            with self.subTest(agent=agent):
                headers = {'User-Agent': agent}
                response = self.client.get('/', headers=headers)
                self.assertEqual(response.status_code, 200)
                self.assertNotIn('noindex', response.headers.get('X-Robots-Tag', '').lower())
                head = HeadParser()
                head.feed(response.get_data(as_text=True))
                icons = head.icons
                self.assertEqual(len(icons), 1)
                self.assertEqual(icons[0]['href'], '/static/img/brand/patia-favicon-96.png')
                self.assertEqual(icons[0]['sizes'], '96x96')
                self.assertEqual(icons[0]['type'], 'image/png')
                self.assertFalse(any('noindex' in value for value in head.robots))
                image_response = self.client.get(icons[0]['href'], headers=headers)
                self.assertEqual(image_response.status_code, 200)
                self.assertEqual(image_response.mimetype, 'image/png')
                self.assertNotIn('noindex', image_response.headers.get('X-Robots-Tag', '').lower())
                self.assertEqual(image_response.data[:8], b'\x89PNG\r\n\x1a\n')
                self.assertEqual(struct.unpack('>II', image_response.data[16:24]), (96, 96))
                # No robots file means there is no application-level crawl exclusion.
                self.assertEqual(self.client.get('/robots.txt', headers=headers).status_code, 404)

    @unittest.skipUnless(importlib.util.find_spec('PIL'), 'Pillow is optional for pixel comparison')
    def test_favicon_is_only_the_official_mark_scaled_without_distortion(self):
        from PIL import Image
        root = Path(self.fixture.app.static_folder) / 'img' / 'brand'
        original = Image.open(root / 'patia-mark-original.png').convert('RGBA')
        original = original.crop(original.getchannel('A').getbbox())
        original.thumbnail((80, 80), Image.Resampling.LANCZOS)
        expected = Image.new('RGBA', (96, 96))
        expected.alpha_composite(original, ((96-original.width)//2, (96-original.height)//2))
        actual = Image.open(root / 'patia-favicon-96.png').convert('RGBA')
        self.assertEqual(actual.tobytes(), expected.tobytes())
        self.assertIsNotNone(actual.resize((16, 16), Image.Resampling.LANCZOS).getchannel('A').getbbox())
