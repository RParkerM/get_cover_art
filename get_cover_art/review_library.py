"""Persistent manual review and safe, per-track application of artwork."""
import csv
import hashlib
import io
import json
import os
import shutil
import tempfile
import threading
from datetime import datetime, timezone
from difflib import SequenceMatcher
from urllib.parse import urlparse

from PIL import Image
from .artwork_audit import audio_paths, inspect_artwork, validate_image
from .catalog import folder_catalog_plan
from .meta import get_meta
from .apple_downloader import AppleDownloader


def identity(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode('utf-8')).hexdigest()


def atomic_json(path, value):
    with open(path + '.part', 'w', encoding='utf-8') as output:
        json.dump(value, output, ensure_ascii=False, indent=2)
    os.replace(path + '.part', path)


def apple_url(url):
    parsed = urlparse(url or '')
    host = (parsed.hostname or '').lower()
    return parsed.scheme == 'https' and not parsed.username and not parsed.port and (
        host.endswith('.mzstatic.com') or host in ('music.apple.com', 'itunes.apple.com'))


def fingerprint(row):
    stat = os.stat(row['path'])
    return dict(metadata=[row.get(k) for k in ('album', 'album_artist', 'track_artist', 'title',
                                             'collection_id', 'track_id')],
                size=stat.st_size, modified=stat.st_mtime_ns, inode=stat.st_ino)


class ReviewLibrary:
    def __init__(self, library, reports, cache, report_files=None, downloader=None):
        self.library = os.path.realpath(library)
        if not os.path.exists(self.library):
            raise ValueError('Library path does not exist')
        self.reports = os.path.abspath(reports)
        self.cache = os.path.abspath(cache)
        os.makedirs(self.reports, exist_ok=True)
        os.makedirs(self.cache, exist_ok=True)
        self.state_path = os.path.join(self.reports, 'artwork_review.json')
        self.lock = threading.RLock()
        self.network_lock = threading.Lock()
        self.downloader = downloader or AppleDownloader(False, 3, 1000, 0, 10)
        self.report_files = report_files
        self.warnings = []
        self.state = {'approvals': {}, 'candidates': {}}
        if os.path.isfile(self.state_path):
            # Never silently discard damaged approvals.
            with open(self.state_path, encoding='utf-8') as source:
                self.state = json.load(source)
            if not isinstance(self.state.get('approvals'), dict) or not isinstance(self.state.get('candidates'), dict):
                raise ValueError('Invalid artwork_review.json; restore its backup before continuing')
        self.updated = 0
        self.worker = None
        self.job = {'running': False, 'results': [], 'total': 0}
        self.scan()

    def save(self):
        atomic_json(self.state_path, self.state)

    def allowed(self, path):
        try:
            return os.path.commonpath([self.library, os.path.realpath(path)]) == self.library
        except ValueError:
            return False

    def scan(self):
        with self.lock:
            if self.job['running']:
                raise ValueError('Wait for the current apply operation')
            self.warnings = []
            self.tracks = {}
            folders = {}
            for path in audio_paths(self.library):
                if not self.allowed(path):
                    self.warnings.append('Skipped audio outside the library: ' + path)
                    continue
                row = inspect_artwork(path)
                row['id'] = identity(os.path.normcase(os.path.realpath(path)))
                self.tracks[row['id']] = row
                folders.setdefault(row['folder'], []).append(row)
            self.groups = {}
            for folder, rows in folders.items():
                plan = folder_catalog_plan(rows)
                ids = sorted({r.get('collection_id') for r in rows if r.get('collection_id')})
                buckets = {}
                for row in rows:
                    effective = plan[row['path']]['collection_id']
                    key = (effective,) if effective else (None, row['album'].strip().casefold(),
                           (row['album_artist'] or row['track_artist']).strip().casefold())
                    buckets.setdefault(key, []).append(row)
                for key, tracks in buckets.items():
                    first = tracks[0]
                    gid = identity([os.path.normcase(folder), key, sorted(t['id'] for t in tracks)])
                    self.groups[gid] = dict(id=gid, album=first['album'], artist=first['album_artist'] or first['track_artist'],
                                           folder=folder, collection_id=key[0], collection_ids=ids,
                                           conflict=len(ids) > 1, unassigned=not key[0] and bool(ids), tracks=tracks,
                                           candidates=[])
            self.load_reports()
            for gid, candidates in self.state['candidates'].items():
                if gid in self.groups:
                    for candidate in candidates:
                        self.add_candidate(self.groups[gid], candidate)
            for group in self.groups.values():
                group['candidates'].sort(key=lambda c: self.rank(group, c))

    def load_reports(self):
        files = self.report_files or [os.path.join(self.reports, name) for name in sorted(os.listdir(self.reports))
                                     if name.lower().endswith('.csv')]
        for filename in files:
            try:
                with open(filename, encoding='utf-8-sig', newline='') as source:
                    reader = csv.DictReader(source)
                    fields = reader.fieldnames or []
                    if 'local_folder' not in fields and 'path' not in fields:
                        continue
                    for row in reader:
                        folder = row.get('local_folder') or row.get('folder') or os.path.dirname(row.get('path') or '')
                        folder = os.path.abspath(os.path.join(os.path.dirname(filename), folder))
                        if row.get('path') and not os.path.isfile(row['path']):
                            self.warnings.append('Report refers to a missing track: ' + row['path'])
                        reported_path = row.get('path')
                        if reported_path:
                            actual = self.tracks.get(identity(os.path.normcase(os.path.realpath(reported_path))))
                            if actual and any(row.get(k) and str(row[k]) != str(actual.get(k) or '')
                                              for k in ('album', 'album_artist', 'track_artist', 'title', 'collection_id', 'track_id')):
                                self.warnings.append('Report metadata differs from the current track: ' + reported_path)
                        matches = [g for g in self.groups.values() if os.path.normcase(g['folder']) == os.path.normcase(folder)
                                   and (row.get('local_album') or row.get('album') or g['album']) == g['album']]
                        candidates = []
                        if row.get('candidate_title') or row.get('collection_id') and 'local_folder' in row:
                            candidates.append(dict(collectionName=row.get('candidate_title'), artistName=row.get('candidate_artist'),
                                                   collectionId=row.get('collection_id'), artworkUrl100=row.get('artwork_url'),
                                                   collectionViewUrl=row.get('collection_url'), reason=row.get('rejection_reason'),
                                                   source=row.get('query') or 'candidate report'))
                        if row.get('candidates'):
                            decoded = json.loads(row['candidates'])
                            if not isinstance(decoded, list) or any(not isinstance(c, dict) for c in decoded):
                                raise ValueError('Invalid candidate JSON')
                            candidates.extend(decoded)
                        cached = row.get('local_cover')
                        if cached:
                            cached = os.path.abspath(os.path.join(os.path.dirname(filename), cached))
                            if self.safe_cached(cached) and os.path.isfile(cached):
                                candidates.append(dict(collectionName=row.get('album'), artistName=row.get('album_artist'),
                                                       local=cached, reason='Previously downloaded cover', source='recovery cache'))
                        for group in matches:
                            for candidate in candidates:
                                self.add_candidate(group, candidate)
            except (OSError, ValueError, csv.Error, TypeError) as error:
                self.warnings.append('Could not fully load {}: {}'.format(filename, error))
        # Existing collection-ID cache names are generated by catalog_cover_path.
        for name in os.listdir(self.cache):
            for group in self.groups.values():
                cid = group['collection_id']
                if cid and name.endswith('.apple-' + cid + '.jpg'):
                    self.add_candidate(group, dict(local=os.path.join(self.cache, name), collectionId=cid,
                                                  collectionName=group['album'], source='collection cache', reason='Cached collection ID'))

    def safe_cached(self, path):
        try:
            return any(os.path.commonpath([root, os.path.realpath(path)]) == root
                       for root in (os.path.realpath(self.cache), self.library))
        except ValueError:
            return False

    def add_candidate(self, group, raw):
        c = dict(raw)
        c['collectionId'] = str(c.get('collectionId') or '')
        c['id'] = identity(['collection', c['collectionId']]) if c['collectionId'] else identity(['image', c.get('artworkUrl100') or c.get('local') or c.get('collectionName') or ''])
        c['verified'] = bool(group['collection_id'] and c['collectionId'] == group['collection_id'])
        existing = next((x for x in group['candidates'] if x['id'] == c['id']), None)
        if existing:
            for key, value in c.items():
                if value and not existing.get(key):
                    existing[key] = value
            if c.get('reason') and c['reason'] not in (existing.get('reason') or ''):
                existing['reason'] = (existing.get('reason') or '') + '; ' + c['reason']
            return existing
        group['candidates'].append(c)
        return c

    def rank(self, group, candidate):
        title = (candidate.get('collectionName') or '').casefold().strip()
        local = group['album'].casefold().strip()
        return (0 if candidate['verified'] else 1 if title == local and title else
                2 if SequenceMatcher(None, title, local).ratio() >= .7 else 3,
                candidate.get('collectionName') or '')

    def group(self, gid):
        if gid not in self.groups:
            raise ValueError('Unknown album group')
        return self.groups[gid]

    def candidate(self, group, cid):
        result = next((c for c in group['candidates'] if c['id'] == cid), None)
        if result is None:
            raise ValueError('Unknown candidate')
        return result

    def cache_image(self, data):
        if len(data) > 12 * 1024 * 1024:
            raise ValueError('Artwork exceeds 12 MB')
        with Image.open(io.BytesIO(data)) as image:
            if image.width * image.height > 25000000:
                raise ValueError('Artwork exceeds 25 million pixels')
        validate_image(data)
        with Image.open(io.BytesIO(data)) as image:
            if image.format not in ('JPEG', 'PNG'):
                raise ValueError('Choose a JPEG or PNG image')
            image.thumbnail((3000, 3000))
            converted = io.BytesIO()
            if image.mode == 'RGBA':
                background = Image.new('RGB', image.size, 'white')
                background.paste(image, mask=image.getchannel('A'))
                image = background
            image.convert('RGB').save(converted, 'JPEG', quality=95)
        data = converted.getvalue()
        path = os.path.join(self.cache, 'review-' + hashlib.sha256(data).hexdigest() + '.jpg')
        if not os.path.isfile(path):
            with open(path + '.part', 'wb') as output:
                output.write(data)
            os.replace(path + '.part', path)
        return path

    def artwork(self, group, candidate, fetch=False):
        local = candidate.get('local')
        if not local:
            saved = next((c for c in group['candidates'] if c['id'] == candidate['id']), {})
            local = saved.get('local')
        if local and self.safe_cached(local) and os.path.isfile(local):
            with open(local, 'rb') as source:
                validate_image(source.read())
            return local
        if not fetch:
            raise ValueError('Artwork is not cached; explicitly fetch the cover first')
        url = candidate.get('artworkUrl100')
        if not apple_url(url):
            raise ValueError('Missing or unsafe Apple artwork URL; use explicit catalog lookup')
        if self.downloader is None:
            raise ValueError('Downloads disabled by --no-download')
        with self.network_lock:
            path = self.cache_image(self.downloader._urlopen_safe(url.replace('100x100bb', self.downloader.file_suffix)))
        candidate['local'] = path
        self.persist_candidates(group)
        return path

    def persist_candidates(self, group):
        self.state['candidates'][group['id']] = group['candidates']
        self.save()

    def upload(self, gid, data):
        with self.lock:
            group = self.group(gid)
            c = self.add_candidate(group, dict(local=self.cache_image(data), collectionName='Local artwork',
                                               source='upload', reason='Manual image; human approval required'))
            self.persist_candidates(group)
            return c

    def search(self, gid, query, kind):
        if not isinstance(query, str) or not query.strip() or len(query) > 300:
            raise ValueError('Enter a search query of 1–300 characters')
        with self.lock:
            group = self.group(gid)
            if self.downloader is None:
                raise ValueError('Catalog requests disabled by --no-download')
            with self.network_lock:
                if kind in ('collection', 'track'):
                    if not query.isdigit() or int(query) <= 0:
                        raise ValueError('Catalog ID must be a positive integer')
                    cid = query if kind == 'collection' else self.downloader._lookup_track_collection(query)
                    candidate = self.downloader._lookup_collection(cid) if cid else None
                    results = [candidate] if candidate else []
                elif kind == 'text':
                    results = self.downloader._query('', query, '')['results']
                else:
                    raise ValueError('Unknown search kind')
            for raw in results:
                self.add_candidate(group, dict(raw, source='Manual ' + kind + ' search', reason='Human review required'))
            group['candidates'].sort(key=lambda c: self.rank(group, c))
            self.persist_candidates(group)
            return group['candidates']

    def approve(self, gid, status, cid=None, targets=None):
        with self.lock:
            if self.job['running']:
                raise ValueError('Wait for the current apply operation')
            group = self.group(gid)
            if status not in ('approved', 'skipped', 'unresolved', 'unreviewed'):
                raise ValueError('Invalid review status')
            if status == 'unreviewed':
                self.state['approvals'].pop(gid, None)
            else:
                entry = dict(status=status, timestamp=datetime.now(timezone.utc).isoformat(), application='pending')
                if status == 'approved':
                    candidate = self.candidate(group, cid)
                    if not isinstance(targets, list) or not targets or any(not isinstance(t, str) for t in targets) or len(set(targets)) != len(targets):
                        raise ValueError('Select the exact target tracks')
                    permitted = {t['id'] for t in group['tracks']}
                    if not set(targets).issubset(permitted):
                        raise ValueError('Targets must belong to this collection group')
                    entry.update(candidate=dict(candidate), targets={tid: fingerprint(self.tracks[tid]) for tid in targets})
                self.state['approvals'][gid] = entry
            self.save()

    def overview(self):
        albums = []
        for group in self.groups.values():
            entry = self.state['approvals'].get(group['id'], {})
            missing = sum(t['status'] == 'missing' for t in group['tracks'])
            embedded = sum(t['status'] == 'embedded' for t in group['tracks'])
            item = {k: v for k, v in group.items() if k not in ('tracks', 'candidates')}
            item.update(missing=missing, embedded=embedded, total=len(group['tracks']),
                        candidate_count=len(group['candidates']), status=entry.get('status', 'unreviewed'),
                        application=entry.get('application', ''), stale=self.stale(group, entry))
            albums.append(item)
        albums.sort(key=lambda g: (-g['missing'], g['album']))
        rows = list(self.tracks.values())
        return dict(albums=albums, warnings=self.warnings,
                    summary=dict(total=len(rows), embedded=sum(t['status'] == 'embedded' for t in rows),
                                 missing=sum(t['status'] == 'missing' for t in rows),
                                 review=sum(g['missing'] > 0 for g in albums), updated=self.updated,
                                 approved=sum(g['status'] == 'approved' for g in albums),
                                 skipped=sum(g['status'] == 'skipped' for g in albums),
                                 unresolved=sum(g['status'] in ('unreviewed', 'unresolved') for g in albums)),
                    orphaned_approvals=[gid for gid in self.state['approvals'] if gid not in self.groups])

    def stale(self, group, entry):
        for tid, old in entry.get('targets', {}).items():
            try:
                if tid not in self.tracks or fingerprint(inspect_artwork(self.tracks[tid]['path'])) != old:
                    return True
            except OSError:
                return True
        return False

    def apply_track(self, tid, old, artwork, dry, replace):
        track = self.tracks.get(tid)
        result = dict(track_id=tid, path=track['path'] if track else '', status='failed', reason='')
        temporary = None
        try:
            if not track or not self.allowed(track['path']) or not os.path.isfile(track['path']):
                result.update(status='stale', reason='Track missing or outside library')
                return result
            path = track['path']
            current = inspect_artwork(path)
            if current['status'] in ('embedded', 'invalid') and not replace:
                result.update(status='skipped', reason='Artwork already exists')
                return result
            if fingerprint(current) != old:
                result.update(status='stale', reason='File or metadata changed since approval')
                return result
            if current['status'] == 'unreadable':
                raise ValueError(current['reason'])
            if not os.access(path, os.R_OK | os.W_OK) or not os.access(os.path.dirname(path), os.W_OK):
                raise ValueError('Audio file or containing folder is not readable/writable')
            if dry:
                result.update(status='would_update', reason='Approved target; original audio will be retained until a verified copy is ready')
                return result
            descriptor, temporary = tempfile.mkstemp(prefix='.artwork-review-', suffix=os.path.splitext(path)[1], dir=os.path.dirname(path))
            os.close(descriptor)
            shutil.copy2(path, temporary)
            meta = get_meta(temporary)
            meta.quiet = True
            if not meta.embed(artwork, detach_existing=replace):
                raise ValueError('Embedding returned no artwork')
            meta.save()
            if inspect_artwork(temporary)['status'] != 'embedded':
                raise ValueError('Written copy failed artwork validation')
            if fingerprint(inspect_artwork(path)) != old:
                result.update(status='stale', reason='Original changed while preparing artwork')
                return result
            os.replace(temporary, path)
            temporary = None
            self.tracks[tid].update(inspect_artwork(path))
            self.updated += 1
            result.update(status='updated', reason='Artwork embedded and verified')
        except Exception as error:
            result['reason'] = str(error)
        finally:
            if temporary and os.path.exists(temporary):
                os.remove(temporary)
        return result

    def run_apply(self, gids, dry=True, replace=False):
        try:
            for gid in gids:
                group = self.group(gid)
                entry = self.state['approvals'].get(gid, {})
                if entry.get('status') != 'approved':
                    continue
                try:
                    artwork = self.artwork(group, entry['candidate'])
                    with open(artwork, 'rb') as source:
                        validate_image(source.read())
                    for tid, old in entry['targets'].items():
                        result = self.apply_track(tid, old, artwork, dry, replace)
                        with self.lock:
                            self.job['results'].append(result)
                            if result['status'] == 'updated':
                                entry['targets'][tid] = fingerprint(self.tracks[tid])
                                self.save()
                    if not dry:
                        entry['application'] = 'applied' if all(r['status'] in ('updated', 'skipped') for r in self.job['results']
                                                               if r['track_id'] in entry['targets']) else 'partial/error'
                except Exception as error:
                    for tid in entry.get('targets', {}):
                        self.job['results'].append(dict(track_id=tid, path=self.tracks.get(tid, {}).get('path', ''),
                                                        status='failed', reason=str(error)))
            with self.lock:
                self.save()
        finally:
            with self.lock:
                self.job['running'] = False

    def start_apply(self, gids, dry=True, replace=False, background=True):
        with self.lock:
            if self.job['running']:
                raise ValueError('An apply operation is already running')
            if not isinstance(gids, list) or not gids or any(not isinstance(g, str) for g in gids) or len(set(gids)) != len(gids):
                raise ValueError('Select album groups')
            for gid in gids:
                self.group(gid)
            self.job = dict(running=True, results=[], total=sum(len(self.state['approvals'].get(gid, {}).get('targets', {})) for gid in gids), dry_run=dry)
        if background:
            self.worker = threading.Thread(target=self.run_apply, args=(gids, dry, replace), daemon=False)
            self.worker.start()
        else:
            self.run_apply(gids, dry, replace)
        return self.job
