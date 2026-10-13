import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from get_cover_art.apple_downloader import AppleDownloader, RateLimitError
from get_cover_art.cover_finder import CoverFinder
from get_cover_art.meta import get_meta
from get_cover_art.__main__ import get_args


def limited(retry_after=None, code=429):
    return HTTPError('https://itunes.apple.com/search', code, 'limited',
                     {'Retry-After': retry_after} if retry_after else {}, None)


class DownloaderTests(unittest.TestCase):
    def setUp(self):
        self.downloader = AppleDownloader(False, 3, 500, 0)
        self.now = 0
        self.clock = patch('get_cover_art.apple_downloader.time.monotonic', side_effect=lambda: self.now)
        self.sleep = patch('get_cover_art.apple_downloader.time.sleep', side_effect=self.advance)
        self.http = patch('get_cover_art.apple_downloader.urlopen')
        self.clock.start()
        self.sleeper = self.sleep.start()
        self.urlopen = self.http.start()
        self.addCleanup(patch.stopall)

    def advance(self, seconds):
        self.now += seconds

    def test_normal_requests_are_paced(self):
        self.urlopen.side_effect = [io.BytesIO(b'one'), io.BytesIO(b'two')]
        self.assertEqual(self.downloader._urlopen_safe('https://itunes.apple.com/search'), b'one')
        self.assertEqual(self.downloader._urlopen_safe('https://example.com/art'), b'two')
        self.sleeper.assert_called_once_with(10)

    def test_three_limits_stop_with_exponential_backoff(self):
        self.downloader.request_interval = 0
        self.urlopen.side_effect = [limited(), limited(), limited()]
        with self.assertRaises(RateLimitError):
            self.downloader._query('artist', 'album', '')
        self.assertEqual(self.urlopen.call_count, 3)
        self.assertEqual([c.args[0] for c in self.sleeper.call_args_list], [3, 6])

    def test_retry_after_seconds_and_success_reset(self):
        self.urlopen.side_effect = [limited('30'), io.BytesIO(b'ok'), limited(), io.BytesIO(b'ok')]
        self.downloader._urlopen_safe('https://itunes.apple.com/search')
        self.assertEqual(self.downloader._rate_limits, 0)
        self.downloader._urlopen_safe('https://itunes.apple.com/search')
        self.assertEqual(self.sleeper.call_args_list[0].args[0], 30)
        self.assertEqual(self.downloader._rate_limits, 0)

    def test_retry_after_http_date(self):
        future = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=60))
        self.urlopen.side_effect = [limited(future), io.BytesIO(b'ok')]
        self.downloader._urlopen_safe('https://itunes.apple.com/search')
        self.assertGreater(self.sleeper.call_args_list[0].args[0], 58)

    def test_invalid_retry_after_uses_backoff(self):
        self.downloader.request_interval = 0
        self.urlopen.side_effect = [limited('invalid', 403), io.BytesIO(b'ok')]
        self.downloader._urlopen_safe('https://itunes.apple.com/search')
        self.sleeper.assert_called_once_with(3)

    def test_network_and_invalid_json_are_not_no_matches(self):
        for response in (URLError('offline'), io.BytesIO(b'invalid json'), io.BytesIO(b'{}')):
            self.urlopen.side_effect = [response]
            with self.assertRaises((URLError, ValueError)):
                self.downloader._query('artist', 'album', '')

    def test_download_search_and_image_are_paced(self):
        result = {'resultCount': 1, 'results': [{'artistName': 'Artist',
                  'collectionName': 'Album', 'artworkUrl100': 'https://example.com/100x100bb.jpg'}]}
        self.urlopen.side_effect = [io.BytesIO(json.dumps(result).encode()), io.BytesIO(b'art')]
        meta = Mock(artist='Artist', album='Album', title='Track')
        with tempfile.TemporaryDirectory(dir=os.path.dirname(__file__)) as folder:
            destination = os.path.join(folder, 'cover.jpg')
            self.assertTrue(self.downloader.download(meta, destination))
            with open(destination, 'rb') as file:
                self.assertEqual(file.read(), b'art')
            self.assertFalse(os.path.exists(destination + '.part'))
        self.sleeper.assert_called_once_with(10)

    def test_image_rate_limit_preserves_existing_download(self):
        self.urlopen.side_effect = [limited(), limited(), limited()]
        with tempfile.TemporaryDirectory(dir=os.path.dirname(__file__)) as folder:
            destination = os.path.join(folder, 'cover.jpg')
            with open(destination, 'wb') as file:
                file.write(b'previous art')
            with self.assertRaises(RateLimitError):
                self.downloader._download_from_url('https://example.com/art', destination)
            with open(destination, 'rb') as file:
                self.assertEqual(file.read(), b'previous art')


class FinderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.path.dirname(__file__))
        self.addCleanup(self.temp.cleanup)
        self.art = os.path.join(self.temp.name, 'Artist - Album.jpg')
        self.cache = os.path.join(self.temp.name, 'skip.txt')
        with open(self.cache, 'w'):
            pass
        self.meta = Mock(artist='Artist', album='Album', title='Track')
        self.meta.has_embedded_art.return_value = False
        self.meta.embed.return_value = True
        self.patch = patch('get_cover_art.cover_finder.get_meta', return_value=self.meta)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.options = dict(art_dest=self.temp.name, skip_artwork=self.cache, test=True)

    def test_temporary_failure_is_retryable_on_next_run(self):
        finder = CoverFinder(self.options)
        finder.downloader.download = Mock(side_effect=URLError('offline'))
        finder.scan_file('track.m4a')
        finder.scan_file('another.m4a')
        finder.downloader.download.assert_called_once()
        self.assertFalse(finder.ignore_artwork.has(self.art))
        resumed = CoverFinder(self.options)
        resumed.downloader.download = Mock(return_value=True)
        resumed.scan_file('track.m4a')
        resumed.downloader.download.assert_called_once()

    def test_rate_limit_stops_folder_scan_without_caching(self):
        finder = CoverFinder(self.options)
        finder.downloader.download = Mock(side_effect=RateLimitError('limited'))
        with patch('os.walk', return_value=[('.', [], ['a.m4a', 'b.m4a'])]):
            finder.scan_folder('.')
        self.assertTrue(finder.stopped)
        finder.downloader.download.assert_called_once()
        self.assertFalse(finder.ignore_artwork.has(self.art))

    def test_cached_art_reused_even_with_old_skip_entry(self):
        with open(self.art, 'wb') as file:
            file.write(b'art')
        finder = CoverFinder(self.options)
        finder.ignore_artwork.add(self.art)
        finder.downloader.download = Mock()
        finder.scan_file('track.m4a')
        finder.downloader.download.assert_not_called()
        self.meta.embed.assert_not_called()
        self.assertEqual(len(finder.files_processed), 1)
        embedder = CoverFinder(dict(self.options, no_download=True, test=False))
        embedder.scan_file('track.m4a')
        self.meta.embed.assert_called_once_with(self.art)
        self.meta.save.assert_called_once()

    def test_embed_failure_does_not_cache_no_match(self):
        self.meta.embed.return_value = False
        finder = CoverFinder(dict(self.options, no_download=True, test=False))
        finder.scan_file('track.m4a')
        self.assertFalse(finder.ignore_artwork.has(self.art))

    def test_confirmed_no_match_is_cached(self):
        finder = CoverFinder(self.options)
        finder.downloader.download = Mock(return_value=False)
        finder.scan_file('track.m4a')
        self.assertTrue(CoverFinder(self.options).ignore_artwork.has(self.art))

    def test_unsupported_file_is_silent(self):
        with patch('get_cover_art.cover_finder.get_meta', wraps=get_meta), patch('builtins.print') as output:
            finder = CoverFinder(self.options)
            finder.scan_file('booklet.pdf')
        output.assert_not_called()
        self.assertEqual(finder.files_invalid, ['booklet.pdf'])

    def test_cli_options(self):
        with patch('sys.argv', ['get_cover_art', '--test', '--no-download', '--art-dest', self.temp.name]):
            args = get_args()
        self.assertTrue(args.test)
        self.assertTrue(args.no_download)
        self.assertEqual(args.art_dest, self.temp.name)
        self.assertEqual(args.request_interval, 10)


if __name__ == '__main__':
    unittest.main()
