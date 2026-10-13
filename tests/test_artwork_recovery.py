import csv
import hashlib
import io
import json
import os
import tempfile
import unittest
import wave
from unittest.mock import patch
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

from PIL import Image
from mutagen.wave import WAVE
from mutagen.id3 import APIC, TALB, TIT2, TPE1, TPE2
from get_cover_art.artwork_audit import audit_artwork, inspect_artwork
from get_cover_art.cover_finder import CoverFinder
from get_cover_art.__main__ import main
from get_cover_art.artwork_review import ManualOverrides, write_candidates
from get_cover_art.catalog import catalog_id
from get_cover_art.meta.meta_mp4 import MetaMP4


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

    def overrides(self, **entry):
        destination = os.path.join(self.root, 'approvals.csv')
        with open(destination, 'w', encoding='utf-8-sig', newline='') as output:
            writer = csv.DictWriter(output, fieldnames=['local_folder', 'local_album', 'local_artist',
                                                       'collection_id', 'artwork_url', 'local_artwork'])
            writer.writeheader()
            writer.writerow(entry)
        return destination

    def test_repeated_tracks_log_one_album_and_export_candidates_once(self):
        self.track('Missing', 'one')
        self.track('Missing', 'two')
        self.http.side_effect = [self.response([self.candidate('Missing Deluxe Edition')])]
        finder = self.finder()
        with patch('builtins.print') as log:
            finder.scan_folder(self.root)
        summaries = [call for call in log.call_args_list if str(call.args[0]).startswith('Album:')]
        self.assertEqual(len(summaries), 1)
        self.assertIn('2 affected tracks', summaries[0].args[0])
        self.assertFalse(any('Missing artwork:' in str(call) or 'Artwork outcome:' in str(call) for call in log.call_args_list))
        self.assertEqual(self.http.call_count, 1)
        report = os.path.join(self.root, 'candidates.csv')
        write_candidates(finder.recovery_rows, report)
        with open(report, encoding='utf-8-sig', newline='') as output:
            candidates = list(csv.DictReader(output))
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]['track_count'], '2')
        self.assertEqual(candidates[0]['collection_id'], '1')
        self.assertIn('exact normalized', candidates[0]['rejection_reason'])
        self.assertIn('itunes.apple.com/search', candidates[0]['query'])

    def test_alternate_editions_and_casts_are_not_accepted(self):
        cases = [('Show', 'Show Deluxe Edition', 'Artist'),
                 ('Show (Original Cast)', 'Show (Revival Cast)', 'Artist'),
                 ('Show (Disc 1)', 'Show', 'Artist'),
                 ('Show', 'Show', 'Artist and Revival Cast')]
        for local, catalog, artist in cases:
            with self.subTest(local=local, catalog=catalog, artist=artist):
                path = self.track(local, 'one')
                self.http.side_effect = [self.response([self.candidate(catalog, artist)])]
                finder = self.finder(retry_unmatched=True)
                finder.scan_file(path)
                self.assertEqual(finder.recovery_rows[0]['status'], 'no_match')
                self.assertEqual(inspect_artwork(path)['status'], 'missing')
                self.assertEqual(len(json.loads(finder.recovery_rows[0]['candidates'])), 1)

    def test_artist_misspelling_fallback_is_review_only(self):
        path = self.track('Show', 'one')
        self.http.side_effect = [self.response([self.candidate('Show', 'Artits')]),
                                 self.response([self.candidate('Show', 'Artist')])]
        finder = self.finder(album_title_fallback=True)
        finder.scan_file(path)
        self.assertEqual(finder.recovery_rows[0]['status'], 'manual_review')
        self.assertEqual(self.http.call_count, 2)
        self.assertEqual(inspect_artwork(path)['status'], 'missing')

    def test_compilation_uses_album_artist_and_downloads_once(self):
        paths = [self.track('Compilation', name) for name in ('one', 'two')]
        for index, path in enumerate(paths):
            audio = WAVE(path)
            audio.tags.add(TPE2(text=['Various Artists']))
            audio.tags.add(TPE1(text=['Performer ' + str(index)]))
            audio.save()
        self.http.side_effect = [self.response([self.candidate('Compilation', 'Various Artists')]), io.BytesIO(self.jpeg)]
        finder = self.finder()
        with patch('builtins.print') as log:
            finder.scan_folder(self.root)
        self.assertEqual(self.http.call_count, 2)
        self.assertTrue(all(inspect_artwork(path)['status'] == 'embedded' for path in paths))
        self.assertEqual(sum(str(call.args[0]).startswith('Album:') for call in log.call_args_list), 1)

    def test_approved_collection_id_lookup_and_offline_reuse(self):
        path = self.track('Show', 'one')
        approval_file = self.overrides(local_album='Show', local_artist='Artist', collection_id='42')
        candidate = self.candidate('Different Approved Recording', 'Cast', 42)
        candidate['wrapperType'] = 'collection'
        self.http.side_effect = [self.response([candidate]), io.BytesIO(self.jpeg)]
        finder = self.finder(artwork_overrides=approval_file, test=True)
        finder.scan_file(path)
        self.assertEqual(self.http.call_count, 2)
        self.assertIn('/lookup?id=42', self.http.call_args_list[0].args[0].full_url)
        self.assertTrue(os.path.isfile(finder.recovery_rows[0]['local_cover']))
        self.assertIn('.apple-42.jpg', finder.recovery_rows[0]['local_cover'])
        self.http.reset_mock()
        offline = self.finder(artwork_overrides=approval_file, no_download=True)
        offline.scan_file(path)
        self.http.assert_not_called()
        self.assertEqual(inspect_artwork(path)['status'], 'embedded')

    def test_approved_url_bypasses_search_and_preserves_old_cover(self):
        path = self.track('Show', 'one')
        old_cover = self.cover('Show')
        old_digest = self.digest(old_cover)
        url = 'https://example.com/approved.jpg'
        approval_file = self.overrides(local_folder=os.path.dirname(path), artwork_url=url)
        self.http.side_effect = [io.BytesIO(self.jpeg)]
        finder = self.finder(artwork_overrides=approval_file)
        finder.scan_file(path)
        self.assertEqual(self.http.call_count, 1)
        self.assertEqual(self.http.call_args.args[0].full_url, url)
        self.assertIn('.url-', finder.recovery_rows[0]['local_cover'])
        self.assertEqual(self.digest(old_cover), old_digest)

    def test_approved_local_image_applies_offline(self):
        path = self.track('Show', 'one')
        image = self.cover('Approved')
        approval_file = self.overrides(local_folder=os.path.dirname(path), local_artwork=os.path.basename(image))
        finder = self.finder(artwork_overrides=approval_file, no_download=True)
        finder.scan_file(path)
        self.assertEqual(inspect_artwork(path)['status'], 'embedded')
        self.http.assert_not_called()

    def test_offline_id_without_cached_image_makes_no_requests(self):
        path = self.track('Show', 'one')
        approval_file = self.overrides(local_folder=os.path.dirname(path), collection_id='42')
        self.cover('Show')  # This unapproved old cover must not stand in for approved ID 42.
        finder = self.finder(artwork_overrides=approval_file, no_download=True)
        finder.scan_file(path)
        self.assertEqual(finder.recovery_rows[0]['status'], 'missing_local_cover')
        self.assertEqual(inspect_artwork(path)['status'], 'missing')
        self.http.assert_not_called()

    def test_bad_or_conflicting_approvals_fail_without_requests(self):
        filename = self.overrides(local_album='Show', local_artist='Artist', collection_id='invalid')
        with self.assertRaises(ValueError):
            ManualOverrides(filename)
        filename = self.overrides(local_album='Show', local_artist='Artist', collection_id='42', artwork_url='https://example.com/art.jpg')
        with self.assertRaises(ValueError):
            ManualOverrides(filename)
        self.http.assert_not_called()

    def test_approval_rate_limit_still_stops_after_three(self):
        path = self.track('Show', 'one')
        filename = self.overrides(local_album='Show', local_artist='Artist', collection_id='42')
        self.http.side_effect = [HTTPError('https://itunes.apple.com/lookup', 429, 'limited', {}, None) for _ in range(3)]
        finder = self.finder(artwork_overrides=filename)
        finder.scan_file(path)
        self.assertTrue(finder.stopped)
        self.assertEqual(self.http.call_count, 3)

    def test_lookup_rejects_unapproved_id(self):
        path = self.track('Show', 'one')
        filename = self.overrides(local_album='Show', local_artist='Artist', collection_id='42')
        candidate = self.candidate('Show', identity=43)
        candidate['wrapperType'] = 'collection'
        self.http.side_effect = [self.response([candidate])]
        finder = self.finder(artwork_overrides=filename)
        finder.scan_file(path)
        self.assertEqual(finder.recovery_rows[0]['status'], 'failed')
        self.assertEqual(self.http.call_count, 1)
        self.assertEqual(inspect_artwork(path)['status'], 'missing')

    def test_cli_writes_per_file_and_candidate_reports(self):
        self.track('Show', 'one')
        self.track('Show', 'two')
        report = os.path.join(self.root, 'recovery.csv')
        candidates = os.path.join(self.root, 'candidates.csv')
        self.http.side_effect = [self.response([self.candidate('Show Deluxe')])]
        with patch('sys.argv', ['get_cover_art', '--path', self.root, '--missing-only',
                               '--art-dest', self.root, '--skip-artwork', self.cache,
                               '--report', report, '--candidate-report', candidates]):
            main()
        with open(report, encoding='utf-8-sig', newline='') as source:
            self.assertEqual(len(list(csv.DictReader(source))), 2)
        with open(candidates, encoding='utf-8-sig', newline='') as source:
            self.assertEqual(len(list(csv.DictReader(source))), 1)

    def purchased_ids(self, mapping):
        # Real temporary audio/embedding, with purchased M4A atoms supplied by the reader.
        def reader(path):
            row = inspect_artwork(path)
            collection, track = mapping.get(os.path.abspath(path), (None, None))
            row.update(collection_id=collection, track_id=track)
            return row
        replacement = patch('get_cover_art.artwork_audit.inspect_artwork', side_effect=reader)
        replacement.start()
        self.addCleanup(replacement.stop)

    def collection(self, identity=80456435):
        item = self.candidate('Catalog Title', 'Catalog Artist', identity)
        item['wrapperType'] = 'collection'
        return item

    def test_mutagen_mp4_catalog_id_extraction(self):
        from mutagen.mp4 import MP4Tags
        tags = MP4Tags()
        tags.update({'plID': [80456435], 'cnID': [80456329], 'atID': [80456331],
                     '©ART': ['Artist'], '©alb': ['Album'], '©nam': ['Track']})
        with patch('get_cover_art.meta.meta_mp4.MP4', return_value=SimpleNamespace(tags=tags)):
            meta = MetaMP4('purchased.m4a')
        self.assertEqual(meta.collection_id, '80456435')
        self.assertEqual(meta.track_id, '80456329')
        self.assertEqual(meta.artist_id, '80456331')
        for invalid in ([], [1, 2], -1, 0, True, 'bad'):
            self.assertIsNone(catalog_id(invalid))

    def test_consistent_catalog_ids_lookup_once_before_text(self):
        paths = [self.track('Local Title', name) for name in ('one', 'two')]
        self.purchased_ids({path: ('80456435', '80456329') for path in paths})
        self.http.side_effect = [self.response([self.collection()]), io.BytesIO(self.jpeg)]
        finder = self.finder()
        finder.scan_folder(self.root)
        self.assertEqual(self.http.call_count, 2)
        self.assertIn('lookup?id=80456435', self.http.call_args_list[0].args[0].full_url)
        self.assertTrue(all(inspect_artwork(path)['status'] == 'embedded' for path in paths))
        self.assertEqual(finder.recovery_rows[0]['lookup_method'], 'collection_id')
        self.assertEqual(finder.recovery_rows[0]['lookup_outcome'], 'resolved')

    def test_missing_id_inherits_consistent_id_including_already_covered_track(self):
        covered = self.track('Show', 'one', self.jpeg)
        missing = self.track('Show', 'two')
        original = self.digest(covered)
        self.purchased_ids({covered: ('42', '101')})
        self.http.side_effect = [self.response([self.collection(42)]), io.BytesIO(self.jpeg)]
        finder = self.finder()
        finder.scan_folder(self.root)
        self.assertEqual(self.digest(covered), original)
        self.assertEqual(inspect_artwork(missing)['status'], 'embedded')
        self.assertEqual(finder.recovery_rows[0]['effective_collection_id'], '42')

    def test_inconsistent_metadata_does_not_inherit_id(self):
        known = self.track('Show', 'one', self.jpeg)
        missing = self.track('Show', 'two')
        audio = WAVE(missing)
        audio.tags.add(TALB(text=['Different Edition']))
        audio.save()
        self.purchased_ids({known: ('42', '101')})
        finder = self.finder()
        finder.scan_folder(self.root)
        self.http.assert_not_called()
        self.assertEqual(finder.recovery_rows[0]['status'], 'manual_review')
        self.assertIn('inconsistent', finder.recovery_rows[0]['id_conflict'])

    def test_conflicting_ids_have_separate_verified_covers_idless_track_needs_review(self):
        paths = [self.track('Show', name) for name in ('one', 'two', 'three')]
        self.purchased_ids({paths[0]: ('42', '101'), paths[1]: ('43', '102')})
        self.cover('Show')  # A generic JPEG must not cross the conflicting ID groups.
        other = io.BytesIO()
        Image.new('RGB', (4, 4), 'red').save(other, 'JPEG')
        self.http.side_effect = [self.response([self.collection(42)]), io.BytesIO(self.jpeg),
                                 self.response([self.collection(43)]), io.BytesIO(other.getvalue())]
        finder = self.finder()
        finder.scan_folder(self.root)
        self.assertEqual(self.http.call_count, 4)
        self.assertEqual(inspect_artwork(paths[0])['status'], 'embedded')
        self.assertEqual(inspect_artwork(paths[1])['status'], 'embedded')
        self.assertNotEqual(WAVE(paths[0]).tags.getall('APIC')[0].data, WAVE(paths[1]).tags.getall('APIC')[0].data)
        self.assertEqual(inspect_artwork(paths[2])['status'], 'missing')
        self.assertTrue(any(row['status'] == 'manual_review' and row['id_conflict'] for row in finder.recovery_rows))

    def test_unresolved_conflicting_id_does_not_text_search(self):
        first = self.track('Show', 'one')
        second = self.track('Show', 'two', self.jpeg)
        self.purchased_ids({first: ('42', None), second: ('43', None)})
        before = self.digest(second)
        self.http.side_effect = [self.response([])]
        finder = self.finder()
        finder.scan_folder(self.root)
        self.assertEqual(self.http.call_count, 1)
        self.assertEqual(finder.recovery_rows[0]['status'], 'manual_review')
        self.assertEqual(self.digest(second), before)

    def test_wrong_collection_wrapper_or_id_falls_back_to_text(self):
        path = self.track('Show', 'one')
        self.purchased_ids({path: ('42', None)})
        wrong = self.collection(43)
        self.http.side_effect = [self.response([wrong]), self.response([self.candidate('Show')]), io.BytesIO(self.jpeg)]
        finder = self.finder()
        finder.scan_file(path)
        self.assertEqual(finder.recovery_rows[0]['lookup_method'], 'title_artist_search')
        self.assertIn('collection unavailable', finder.recovery_rows[0]['lookup_outcome'])
        self.assertIn('text match', finder.recovery_rows[0]['lookup_outcome'])
        self.assertEqual(self.http.call_count, 3)

    def test_optional_track_lookup_validates_associated_collection(self):
        path = self.track('Show', 'one')
        self.purchased_ids({path: ('42', '101')})
        track = dict(wrapperType='track', trackId=101, collectionId=43)
        self.http.side_effect = [self.response([]), self.response([track]),
                                 self.response([self.collection(43)]), io.BytesIO(self.jpeg)]
        finder = self.finder(track_id_fallback=True)
        finder.scan_file(path)
        row = finder.recovery_rows[0]
        self.assertEqual(row['lookup_method'], 'track_id')
        self.assertEqual(row['resolved_collection_id'], '43')
        self.assertEqual(self.http.call_count, 4)

    def test_catalog_test_mode_then_offline_embedding(self):
        path = self.track('Show', 'one')
        self.purchased_ids({path: ('42', None)})
        before = self.digest(path)
        self.http.side_effect = [self.response([self.collection(42)]), io.BytesIO(self.jpeg)]
        finder = self.finder(test=True)
        finder.scan_file(path)
        self.assertEqual(self.digest(path), before)
        self.http.reset_mock()
        offline = self.finder(no_download=True)
        offline.scan_file(path)
        self.http.assert_not_called()
        self.assertEqual(inspect_artwork(path)['status'], 'embedded')

    def test_catalog_lookup_failure_keeps_cache_retryable_and_stops_on_limits(self):
        path = self.track('Show', 'one')
        self.purchased_ids({path: ('42', None)})
        self.http.side_effect = [HTTPError('https://itunes.apple.com/lookup', 429, 'limited', {}, None) for _ in range(3)]
        finder = self.finder()
        finder.scan_file(path)
        self.assertTrue(finder.stopped)
        self.assertEqual(self.http.call_count, 3)
        self.assertFalse(finder.ignore_artwork.has(finder.recovery_rows[0]['local_cover']))

    def test_lookup_cache_shared_between_folders(self):
        first = self.track('First Local Title', 'one')
        second = self.track('Second Local Title', 'one')
        self.purchased_ids({first: ('42', None), second: ('42', None)})
        self.http.side_effect = [self.response([self.collection(42)]), io.BytesIO(self.jpeg), io.BytesIO(self.jpeg)]
        finder = self.finder()
        finder.scan_folder(self.root)
        urls = [call.args[0].full_url for call in self.http.call_args_list]
        self.assertEqual(sum('/lookup?' in url for url in urls), 1)

    def test_default_scan_also_uses_catalog_id(self):
        path = self.track('Show', 'one')
        self.purchased_ids({path: ('42', None)})
        self.http.side_effect = [self.response([self.collection(42)]), io.BytesIO(self.jpeg)]
        finder = CoverFinder(dict(art_dest=self.root, request_interval=0))
        finder.scan_file(path)
        self.assertEqual(finder.recovery_rows[0]['lookup_method'], 'collection_id')
        self.assertEqual(inspect_artwork(path)['status'], 'embedded')

    def test_lookup_collection_requires_collection_wrapper(self):
        path = self.track('Show', 'one')
        self.purchased_ids({path: ('42', None)})
        item = self.collection(42)
        item['wrapperType'] = 'track'
        self.http.side_effect = [self.response([item]), self.response([])]
        finder = self.finder()
        finder.scan_file(path)
        self.assertEqual(inspect_artwork(path)['status'], 'missing')
        self.assertEqual(finder.recovery_rows[0]['lookup_method'], 'title_artist_search')
        self.assertEqual(finder.recovery_rows[0]['status'], 'no_match')

    def test_cached_text_fallback_does_not_become_verified_conflict_cover(self):
        first = self.track('Show', 'one')
        self.purchased_ids({first: ('42', None)})
        self.http.side_effect = [self.response([]), self.response([self.candidate('Show')]), io.BytesIO(self.jpeg)]
        finder = self.finder(test=True)
        finder.scan_file(first)
        self.assertIn('.search.jpg', finder.recovery_rows[0]['local_cover'])
        second = self.track('Show', 'two', self.jpeg)
        self.purchased_ids({first: ('42', None), second: ('43', None)})
        # Later discovery of a conflicting ID must exclude the earlier text-only cover.
        self.http.reset_mock()
        self.http.side_effect = [self.response([])]
        resumed = self.finder()
        resumed.scan_folder(self.root)
        self.assertEqual(self.http.call_count, 1)
        self.assertEqual(resumed.recovery_rows[0]['status'], 'manual_review')
        self.assertEqual(inspect_artwork(first)['status'], 'missing')

    def test_negative_and_temporary_lookups_are_cached_only_for_run(self):
        from get_cover_art.apple_downloader import AppleDownloader
        downloader = AppleDownloader(False, 3, 500, 0, 0)
        self.http.side_effect = [self.response([])]
        self.assertIsNone(downloader._lookup_collection('42'))
        self.assertIsNone(downloader._lookup_collection('42'))
        self.assertEqual(self.http.call_count, 1)
        self.http.reset_mock()
        self.http.side_effect = [URLError('offline')]
        for _ in range(2):
            with self.assertRaises(URLError):
                downloader._lookup_collection('43')
        self.assertEqual(self.http.call_count, 1)
        self.assertEqual(AppleDownloader(False, 3, 500, 0).lookup_cache, {})

    def test_track_lookup_rejects_wrong_track_id(self):
        path = self.track('Show', 'one')
        self.purchased_ids({path: ('42', '101')})
        self.http.side_effect = [self.response([]), self.response([dict(wrapperType='track', trackId=102, collectionId=43)]),
                                 self.response([])]
        finder = self.finder(track_id_fallback=True)
        finder.scan_file(path)
        self.assertEqual(finder.recovery_rows[0]['lookup_method'], 'title_artist_search')
        self.assertEqual(inspect_artwork(path)['status'], 'missing')
        self.assertFalse(any('id=43' in call.args[0].full_url for call in self.http.call_args_list))

    def test_recovery_report_contains_catalog_diagnostics(self):
        path = self.track('Show', 'one')
        self.purchased_ids({path: ('42', None)})
        self.http.side_effect = [self.response([self.collection(42)]), io.BytesIO(self.jpeg)]
        report = os.path.join(self.root, 'recovery.csv')
        candidates = os.path.join(self.root, 'candidates.csv')
        with patch('sys.argv', ['get_cover_art', '--path', self.root, '--art-dest', self.root, '--missing-only',
                               '--report', report, '--candidate-report', candidates]):
            main()
        with open(report, encoding='utf-8-sig', newline='') as source:
            row = next(csv.DictReader(source))
        self.assertEqual(row['effective_collection_id'], '42')
        self.assertEqual(row['resolved_collection_id'], '42')
        self.assertEqual(row['lookup_method'], 'collection_id')
        self.assertEqual(row['lookup_outcome'], 'resolved')

    def test_idless_conflicting_track_can_use_its_own_verified_track_id(self):
        first = self.track('Show', 'one', self.jpeg)
        second = self.track('Show', 'two', self.jpeg)
        missing = self.track('Show', 'three')
        self.purchased_ids({first: ('42', None), second: ('43', None), missing: (None, '101')})
        self.http.side_effect = [self.response([dict(wrapperType='track', trackId=101, collectionId=43)]),
                                 self.response([self.collection(43)]), io.BytesIO(self.jpeg)]
        finder = self.finder(track_id_fallback=True)
        finder.scan_folder(self.root)
        self.assertEqual(inspect_artwork(missing)['status'], 'embedded')
        self.assertEqual(finder.recovery_rows[0]['lookup_method'], 'track_id')
        self.assertEqual(finder.recovery_rows[0]['resolved_collection_id'], '43')


if __name__ == '__main__':
    unittest.main()
