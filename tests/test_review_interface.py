import csv
import io
import json
import os
import re
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from mutagen.wave import WAVE
from mutagen.id3 import TALB, TIT2, TPE1, TPE2, APIC
from get_cover_art.review_library import ReviewLibrary, apple_url
from get_cover_art.review_server import create_app
from get_cover_art.artwork_audit import inspect_artwork
from get_cover_art.apple_downloader import RateLimitError


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.path.dirname(__file__))
        self.addCleanup(self.temp.cleanup)
        self.root = self.temp.name
        self.music = os.path.join(self.root, 'music')
        self.reports = os.path.join(self.root, 'reports')
        self.cache = os.path.join(self.root, 'cache')
        os.makedirs(self.music)
        os.makedirs(self.reports)
        image = io.BytesIO()
        Image.new('RGBA', (8, 8), (0, 0, 255, 128)).save(image, 'PNG')
        self.png = image.getvalue()
        self.p1 = self.track('one')
        self.p2 = self.track('two')
        network = patch('get_cover_art.apple_downloader.urlopen', side_effect=AssertionError('No live HTTP allowed'))
        network.start()
        self.addCleanup(network.stop)
        self.lib = self.new_library()

    def track(self, name, art=False):
        path = os.path.join(self.music, name + '.wav')
        with wave.open(path, 'wb') as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(8000)
            audio.writeframes(b'\0\0' * 80)
        audio = WAVE(path)
        audio.add_tags()
        for tag in (TALB(text=['Album']), TIT2(text=[name]), TPE1(text=['Cast']), TPE2(text=['Artist'])):
            audio.tags.add(tag)
        if art:
            image = io.BytesIO()
            Image.new('RGB', (4, 4)).save(image, 'JPEG')
            audio.tags.add(APIC(mime='image/jpeg', type=3, data=image.getvalue()))
        audio.save()
        return path

    def new_library(self):
        return ReviewLibrary(self.music, self.reports, self.cache)

    def group(self):
        return next(iter(self.lib.groups.values()))

    def approve(self, targets=None):
        group = self.group()
        candidate = self.lib.upload(group['id'], self.png)
        self.lib.approve(group['id'], 'approved', candidate['id'], targets or [t['id'] for t in group['tracks']])
        return group['id'], candidate

    def report(self, name, rows):
        with open(os.path.join(self.reports, name), 'w', encoding='utf-8-sig', newline='') as source:
            writer = csv.DictWriter(source, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    def test_candidate_csv_dedupe_ranking_and_diagnostics(self):
        row = dict(local_folder=self.music, local_album='Album', candidate_title='Album', candidate_artist='Other',
                   collection_id='123', artwork_url='https://is1-ssl.mzstatic.com/image/100x100bb.jpg',
                   rejection_reason='artist mismatch', collection_url='https://music.apple.com/album/123')
        self.report('custom_candidates.csv', [row, row])
        self.lib.scan()
        group = self.group()
        self.assertEqual(len(group['candidates']), 1)
        self.assertIn('artist mismatch', group['candidates'][0]['reason'])
        exact = group['candidates'][0]
        verified = self.lib.add_candidate(group, dict(collectionId='999', collectionName='Different', reason='ID'))
        group['collection_id'] = '999'
        verified['verified'] = True
        self.assertLess(self.lib.rank(group, verified), self.lib.rank(group, exact))
        strong = dict(verified=False, collectionName='Album edition')
        other = dict(verified=False, collectionName='Unrelated')
        self.assertLess(self.lib.rank(group, exact), self.lib.rank(group, other))

    def test_recovery_cache_missing_and_malformed_reports(self):
        cover = self.lib.cache_image(self.png)
        self.report('artwork_recovery.csv', [dict(path=self.p1, folder=self.music, album='Album', album_artist='Artist',
                                               local_cover=cover, candidates=json.dumps([dict(collectionId=42, collectionName='Album', reason='rejected')]))])
        self.lib.scan()
        self.assertEqual(len(self.group()['candidates']), 2)
        self.report('bad.csv', [dict(path=self.p1, folder=self.music, candidates='{')])
        self.report('moved.csv', [dict(path=self.p1+'.gone', folder=self.music)])
        self.lib.scan()
        self.assertTrue(self.lib.warnings)
        with open(os.path.join(self.reports, 'empty.csv'), 'w'):
            pass
        self.lib.scan()

    def test_persistent_approval_change_revoke(self):
        gid, candidate = self.approve()
        restored = self.new_library()
        self.assertEqual(restored.state['approvals'][gid]['status'], 'approved')
        restored.approve(gid, 'skipped')
        self.assertEqual(self.new_library().state['approvals'][gid]['status'], 'skipped')
        restored.approve(gid, 'unresolved')
        restored.approve(gid, 'unreviewed')
        self.assertNotIn(gid, self.new_library().state['approvals'])

    def test_upload_content_validation_and_png_conversion(self):
        candidate = self.lib.upload(self.group()['id'], self.png)
        with Image.open(candidate['local']) as image:
            self.assertEqual(image.format, 'JPEG')
        with self.assertRaises(Exception):
            self.lib.upload(self.group()['id'], b'not an image.jpg')
        with self.assertRaises(ValueError):
            self.lib.upload(self.group()['id'], b'x' * (12 * 1024 * 1024 + 1))

    def test_dry_run_apply_and_repeat_preserves_existing(self):
        gid, _ = self.approve()
        original = Path(self.p1).read_bytes()
        self.lib.start_apply([gid], background=False)
        self.assertEqual(Path(self.p1).read_bytes(), original)
        self.assertEqual({r['status'] for r in self.lib.job['results']}, {'would_update'})
        self.lib.start_apply([gid], dry=False, background=False)
        self.assertEqual(self.lib.updated, 2)
        self.assertFalse(self.lib.overview()['albums'][0]['stale'])
        self.assertEqual(inspect_artwork(self.p1)['status'], 'embedded')
        written = Path(self.p1).read_bytes()
        self.lib.start_apply([gid], dry=False, background=False)
        self.assertEqual({r['status'] for r in self.lib.job['results']}, {'skipped'})
        self.assertEqual(Path(self.p1).read_bytes(), written)

    def test_partial_coverage_preserved(self):
        self.track('covered', art=True)
        self.lib.scan()
        summary = self.lib.overview()
        self.assertEqual(summary['albums'][0]['embedded'], 1)
        gid, _ = self.approve()
        self.lib.start_apply([gid], dry=False, background=False)
        self.assertEqual([r['status'] for r in self.lib.job['results']].count('skipped'), 1)

    def test_stale_and_missing_tracks(self):
        gid, _ = self.approve()
        audio = WAVE(self.p1)
        audio.tags.add(TALB(text=['Changed']))
        audio.save()
        os.remove(self.p2)
        self.lib.start_apply([gid], dry=False, background=False)
        self.assertEqual({r['status'] for r in self.lib.job['results']}, {'stale'})
        self.lib.scan()
        self.assertIn(gid, self.lib.overview()['orphaned_approvals'])

    def test_failed_writer_original_unchanged_and_other_track_continues(self):
        gid, _ = self.approve()
        before = Path(self.p1).read_bytes()
        from get_cover_art.review_library import get_meta
        calls = [0]
        def writer(path):
            calls[0] += 1
            if calls[0] == 1:
                raise OSError('mock write failure')
            return get_meta(path)
        with patch('get_cover_art.review_library.get_meta', side_effect=writer):
            self.lib.start_apply([gid], dry=False, background=False)
        self.assertEqual(Path(self.p1).read_bytes(), before)
        self.assertEqual([r['status'] for r in self.lib.job['results']], ['failed', 'updated'])
        self.assertFalse(any(name.startswith('.artwork-review-') for name in os.listdir(self.music)))

    def test_conflict_groups_and_no_id_selection(self):
        original = inspect_artwork
        def inspect(path):
            row = original(path)
            row['collection_id'] = '1' if path == self.p1 else '2' if path == self.p2 else None
            return row
        self.track('no-id')
        with patch('get_cover_art.review_library.inspect_artwork', side_effect=inspect):
            self.lib.scan()
        self.assertEqual(len(self.lib.groups), 3)
        groups = list(self.lib.groups.values())
        self.assertTrue(all(g['conflict'] for g in groups))
        unassigned = next(g for g in groups if g['unassigned'])
        c = self.lib.upload(unassigned['id'], self.png)
        with self.assertRaises(ValueError):
            self.lib.approve(unassigned['id'], 'approved', c['id'], [])
        with self.assertRaises(ValueError):
            self.lib.approve(unassigned['id'], 'approved', c['id'], [next(g for g in groups if g['collection_id'])['tracks'][0]['id']])

    def test_agreeing_id_inheritance(self):
        original = inspect_artwork
        def inspect(path):
            row = original(path)
            row['collection_id'] = '1' if path == self.p1 else None
            return row
        with patch('get_cover_art.review_library.inspect_artwork', side_effect=inspect):
            self.lib.scan()
        self.assertEqual(len(self.lib.groups), 1)
        self.assertEqual(self.group()['collection_id'], '1')

    def test_explicit_search_and_network_failure(self):
        gid = self.group()['id']
        with patch.object(self.lib.downloader, '_query', return_value={'results':[dict(collectionId=12, collectionName='Album')]}):
            self.lib.search(gid, 'Album Cast', 'text')
        self.assertEqual(len(self.new_library().groups[gid]['candidates']), 1)
        with patch.object(self.lib.downloader, '_query', side_effect=RateLimitError('rate limited')):
            with self.assertRaises(RateLimitError):
                self.lib.search(gid, 'Album', 'text')
        with self.assertRaises(ValueError):
            self.lib.search(gid, '../bad', 'collection')

    def test_http_security_escaping_and_paths(self):
        app = create_app(self.lib)
        client = app.test_client()
        base = 'http://127.0.0.1'
        page = client.get('/', base_url=base)
        self.assertEqual(page.status_code, 200)
        token = re.search(b'name="review-token" content="([a-f0-9]+)"', page.data).group(1).decode()
        headers = {'X-Review-Token':token}
        self.assertEqual(client.post('/api/refresh', base_url=base).status_code, 403)
        self.assertEqual(client.post('/api/refresh', base_url=base, headers=dict(headers, Origin='http://evil.example')).status_code, 403)
        self.assertEqual(client.get('/api/library', base_url='http://evil.example').status_code, 403)
        self.assertEqual(client.get('/api/group/arbitrary/image/path', base_url=base).status_code, 400)
        self.assertEqual(client.post('/api/apply', base_url=base, headers=headers, json={'groups':['../outside']}).status_code, 400)
        self.assertEqual(client.get('/api/library', base_url=base).status_code, 200)
        self.assertIn('frame-ancestors', page.headers['Content-Security-Policy'])
        response = client.get('/static/review.js', base_url=base)
        script = response.data.decode('utf-8-sig')
        response.close()
        self.assertNotIn('innerHTML', script)
        self.assertIn('textContent', script)
        upload = client.post('/api/group/'+self.group()['id']+'/upload', base_url=base, headers=headers,
                             data={'image':(io.BytesIO(self.png), '../../outside.png')})
        self.assertEqual(upload.status_code, 200)
        self.assertTrue(upload.json['local'].startswith(self.cache))

    def test_changed_cover_and_specific_targets(self):
        gid, first = self.approve()
        image = io.BytesIO()
        Image.new('RGB', (5, 5), 'red').save(image, 'JPEG')
        second = self.lib.upload(gid, image.getvalue())
        target = self.group()['tracks'][0]['id']
        self.lib.approve(gid, 'approved', second['id'], [target])
        restored = self.new_library()
        self.assertEqual(restored.state['approvals'][gid]['candidate']['id'], second['id'])
        restored.start_apply([gid], dry=False, background=False)
        self.assertEqual(restored.updated, 1)
        self.assertEqual(inspect_artwork(self.p2)['status'], 'missing')

    def test_fetch_after_approval_reuses_paced_downloader_and_cache(self):
        group = self.group()
        c = self.lib.add_candidate(group, dict(collectionId='123', collectionName='Album',
                                  artworkUrl100='https://is1-ssl.mzstatic.com/image/100x100bb.jpg'))
        self.lib.approve(group['id'], 'approved', c['id'], [t['id'] for t in group['tracks']])
        with self.assertRaises(ValueError):
            self.lib.artwork(group, c)
        with patch.object(self.lib.downloader, '_urlopen_safe', return_value=self.png) as request:
            self.lib.artwork(group, c, fetch=True)
            request.assert_called_once()
        self.lib.start_apply([group['id']], dry=False, background=False)
        self.assertEqual(self.lib.updated, 2)

    def test_unsafe_urls_and_cache_paths_rejected(self):
        for url in ('http://is1.mzstatic.com/a', 'https://evil.com/a', 'https://is1.mzstatic.com.evil.com/a',
                    'https://user@is1.mzstatic.com/a'):
            self.assertFalse(apple_url(url))
        group = self.group()
        c = self.lib.add_candidate(group, dict(local=os.path.join(self.root, 'secret.jpg')))
        with self.assertRaises(ValueError):
            self.lib.artwork(group, c)
        self.lib.downloader = None
        with self.assertRaises(ValueError):
            self.lib.search(group['id'], 'Album', 'text')

    def test_corrupt_persistent_state_stops_startup(self):
        with open(self.lib.state_path, 'w') as output:
            output.write('{')
        with self.assertRaises(ValueError):
            self.new_library()

    def test_second_server_lock_rejected_and_released(self):
        from get_cover_art.review_lock import ReviewInstance
        with ReviewInstance(self.music, self.reports):
            with self.assertRaises(ValueError):
                with ReviewInstance(self.music, self.reports):
                    pass
        with ReviewInstance(self.music, self.reports):
            pass

    def test_overlapping_operations_rejected(self):
        gid, _ = self.approve()
        self.lib.job['running'] = True
        with self.assertRaises(ValueError):
            self.lib.start_apply([gid])
        with self.assertRaises(ValueError):
            self.lib.scan()
        with self.assertRaises(ValueError):
            self.lib.approve(gid, 'unreviewed')


if __name__ == '__main__':
    unittest.main()

