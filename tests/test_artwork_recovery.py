import csv
import hashlib
import io
import json
import os
import tempfile
import unittest
import wave
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from PIL import Image
from mutagen.wave import WAVE
from mutagen.id3 import APIC, TALB, TIT2, TPE1, TPE2
from get_cover_art.artwork_audit import audit_artwork, inspect_artwork
from get_cover_art.cover_finder import CoverFinder
from get_cover_art.__main__ import main


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.path.dirname(__file__))
        self.addCleanup(self.temp.cleanup)
        self.root = self.temp.name
        image = io.BytesIO()
        Image.new('RGB', (4, 4), 'blue').save(image, 'JPEG')
        self.jpeg = image.getvalue()
        self.cache = os.path.join(self.root, 'skip.txt')
        with open(self.cache, 'w'):
            pass
        self.http_patch = patch('get_cover_art.apple_downloader.urlopen', side_effect=AssertionError('Unexpected HTTP request'))
        self.http = self.http_patch.start()
        self.addCleanup(self.http_patch.stop)
        self.sleep_patch = patch('get_cover_art.apple_downloader.time.sleep')
        self.sleep_patch.start()
        self.addCleanup(self.sleep_patch.stop)

    def track(self, album, name, art=None):
        folder = os.path.join(self.root, album)
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, name + '.wav')
        with wave.open(path, 'wb') as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(8000)
            audio.writeframes(b'\x00\x00' * 80)
        audio = WAVE(path)
        audio.add_tags()
        for tag in (TALB(text=[album]), TIT2(text=[name]), TPE1(text=['Track Artist']), TPE2(text=['Artist'])):
            audio.tags.add(tag)
        if art is not None:
            audio.tags.add(APIC(mime='image/jpeg', type=3, data=art))
        audio.save()
        return path

    def finder(self, **options):
        return CoverFinder(dict(missing_only=True, art_dest=self.root, skip_artwork=self.cache,
                                request_interval=0, **options))

    def cover(self, album):
        path = os.path.join(self.root, 'Artist - ' + album + '.jpg')
        with open(path, 'wb') as output:
            output.write(self.jpeg)
        return path

    def response(self, candidates):
        return io.BytesIO(json.dumps(dict(resultCount=len(candidates), results=candidates)).encode())

    def candidate(self, album, artist='Artist', identity=1):
        return dict(collectionName=album, artistName=artist, collectionId=identity,
                    artworkUrl100='https://example.com/100x100bb.jpg')

    def digest(self, path):
        with open(path, 'rb') as audio:
            return hashlib.sha256(audio.read()).hexdigest()

    def test_audit_real_audio_complete_missing_partial_invalid_and_pdf(self):
        paths = [self.track('Complete', 'one', self.jpeg), self.track('Missing', 'one'),
                 self.track('Partial', 'one', self.jpeg), self.track('Partial', 'two'),
                 self.track('Invalid', 'one', b'bad image')]
        with open(os.path.join(self.root, 'booklet.pdf'), 'wb') as output:
            output.write(b'pdf')
        broken = os.path.join(self.root, 'broken.m4a')
        with open(broken, 'wb') as output:
            output.write(b'not audio')
        before = {path: self.digest(path) for path in paths + [broken]}
        report = os.path.join(self.root, 'audit.csv')
        rows = audit_artwork(self.root, report)
        self.assertEqual(len(rows), 6)
        self.assertEqual(sum(row['status'] == 'missing' for row in rows), 2)
        self.assertEqual(sum(row['status'] == 'invalid' for row in rows), 1)
        partial = [row for row in rows if row['album'] == 'Partial']
        self.assertEqual(partial[0]['folder_embedded'], 1)
        self.assertEqual(partial[0]['folder_missing'], 1)
        with open(report, encoding='utf-8-sig', newline='') as output:
            affected = list(csv.DictReader(output))
        self.assertEqual(len(affected), 4)
        self.assertEqual(before, {path: self.digest(path) for path in before})
        self.http.assert_not_called()

    def test_cli_audit_does_not_construct_finder(self):
        self.track('Missing', 'one')
        report = os.path.join(self.root, 'audit.csv')
        with patch('sys.argv', ['get_cover_art', '--path', self.root, '--audit-artwork', '--report', report]), \
                patch('get_cover_art.__main__.CoverFinder') as constructor:
            main()
        constructor.assert_not_called()
        self.http.assert_not_called()

    def test_partial_recovery_preserves_existing_art_and_reuses_jpeg(self):
        existing = self.track('Partial', 'one', self.jpeg)
        missing = self.track('Partial', 'two')
        complete = self.track('Complete', 'one', self.jpeg)
        before = self.digest(existing), self.digest(complete)
        self.cover('Partial')
        finder = self.finder(no_download=True)
        finder.scan_folder(self.root)
        self.assertEqual(inspect_artwork(missing)['status'], 'embedded')
        self.assertEqual(before, (self.digest(existing), self.digest(complete)))
        self.assertEqual(len(finder.files_processed), 1)
        self.http.assert_not_called()

    def test_cached_miss_does_not_request_unless_explicitly_retried(self):
        path = self.track('Missing', 'one')
        finder = self.finder()
        art = os.path.join(self.root, 'Artist - Missing.jpg')
        finder.ignore_artwork.add(art)
        finder.scan_file(path)
        self.assertEqual(finder.recovery_rows[0]['status'], 'cached_no_match')
        self.http.assert_not_called()
        self.http.side_effect = [self.response([self.candidate('Missing')]), io.BytesIO(self.jpeg)]
        resumed = self.finder(retry_unmatched=True)
        resumed.scan_file(path)
        self.assertEqual(inspect_artwork(path)['status'], 'embedded')
        self.assertEqual(self.http.call_count, 2)
        self.assertIn('itunes.apple.com/search', resumed.recovery_rows[0]['queries'])

    def test_cached_miss_with_jpeg_uses_local_cover(self):
        path = self.track('Missing', 'one')
        art = self.cover('Missing')
        finder = self.finder()
        finder.ignore_artwork.add(art)
        finder.scan_file(path)
        self.assertEqual(inspect_artwork(path)['status'], 'embedded')
        self.http.assert_not_called()

    def test_ambiguous_matches_require_review_not_download_or_cache(self):
        path = self.track('Missing', 'one')
        self.http.side_effect = [self.response([self.candidate('Missing', identity=1), self.candidate('Missing', identity=2)])]
        finder = self.finder()
        finder.scan_file(path)
        self.assertEqual(finder.recovery_rows[0]['status'], 'manual_review')
        self.assertEqual(self.http.call_count, 1)
        self.assertFalse(finder.ignore_artwork.has(os.path.join(self.root, 'Artist - Missing.jpg')))
        self.assertEqual(inspect_artwork(path)['status'], 'missing')

    def test_fallback_reports_artist_mismatch_candidates_only(self):
        path = self.track('Missing', 'one')
        self.http.side_effect = [self.response([self.candidate('Missing', 'Cast')]),
                                 self.response([self.candidate('Missing', 'Cast')])]
        finder = self.finder(album_title_fallback=True)
        finder.scan_file(path)
        row = finder.recovery_rows[0]
        self.assertEqual(row['status'], 'manual_review')
        self.assertIn('artist mismatch', row['candidates'])
        self.assertEqual(len(json.loads(row['queries'])), 2)
        self.assertEqual(inspect_artwork(path)['status'], 'missing')

    def test_no_match_is_cached_and_queried_once_per_album(self):
        self.track('Missing', 'one')
        self.track('Missing', 'two')
        self.http.side_effect = [self.response([])]
        finder = self.finder()
        finder.scan_folder(self.root)
        self.assertEqual(self.http.call_count, 1)
        self.assertTrue(finder.ignore_artwork.has(os.path.join(self.root, 'Artist - Missing.jpg')))
        self.assertTrue(all(row['status'] == 'no_match' for row in finder.recovery_rows))

    def test_temporary_failure_does_not_poison_cache(self):
        self.track('Missing', 'one')
        self.track('Missing', 'two')
        self.http.side_effect = [URLError('offline')]
        finder = self.finder()
        finder.scan_folder(self.root)
        self.assertEqual(self.http.call_count, 1)
        self.assertFalse(finder.ignore_artwork.has(os.path.join(self.root, 'Artist - Missing.jpg')))
        self.assertEqual(len(finder.files_failed), 2)

    def test_rate_limit_stops_recovery_and_keeps_cache_retryable(self):
        self.track('Missing', 'one')
        self.track('Missing', 'two')
        self.http.side_effect = [HTTPError('https://itunes.apple.com/search', 429, 'limited', {}, None) for _ in range(3)]
        finder = self.finder()
        finder.scan_folder(self.root)
        self.assertTrue(finder.stopped)
        self.assertEqual(self.http.call_count, 3)
        self.assertEqual(len(finder.recovery_rows), 1)
        self.assertFalse(finder.ignore_artwork.has(os.path.join(self.root, 'Artist - Missing.jpg')))

    def test_invalid_existing_art_is_reported_not_overwritten(self):
        path = self.track('Invalid', 'one', b'broken')
        before = self.digest(path)
        finder = self.finder()
        finder.scan_file(path)
        self.assertEqual(finder.recovery_rows[0]['status'], 'manual_review')
        self.assertEqual(before, self.digest(path))
        self.http.assert_not_called()

    def test_test_mode_keeps_audio_unchanged(self):
        path = self.track('Missing', 'one')
        self.cover('Missing')
        before = self.digest(path)
        finder = self.finder(test=True)
        finder.scan_file(path)
        self.assertEqual(before, self.digest(path))
        self.assertEqual(finder.recovery_rows[0]['status'], 'cover_available')

    def test_missing_artist_is_auditable_and_reported_without_requests(self):
        path = self.track('Missing', 'one')
        audio = WAVE(path)
        audio.tags.delall('TPE1')
        audio.tags.delall('TPE2')
        audio.save()
        self.assertEqual(inspect_artwork(path)['status'], 'missing')
        finder = self.finder()
        finder.scan_file(path)
        self.assertEqual(finder.recovery_rows[0]['status'], 'failed')
        self.assertIn('missing ID3 tags', finder.recovery_rows[0]['reason'])
        self.http.assert_not_called()

    def test_invalid_cached_jpeg_does_not_modify_audio(self):
        path = self.track('Missing', 'one')
        art = self.cover('Missing')
        with open(art, 'wb') as output:
            output.write(b'broken jpeg')
        before = self.digest(path)
        finder = self.finder()
        finder.scan_file(path)
        self.assertEqual(finder.recovery_rows[0]['status'], 'failed')
        self.assertEqual(self.digest(path), before)
        self.http.assert_not_called()


if __name__ == '__main__':
    unittest.main()
