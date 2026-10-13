import json
import os
import time
import unicodedata
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from urllib.request import Request, urlopen, HTTPError
from urllib.parse import urlparse, quote
from typing import Tuple
from .normalizer import ArtistNormalizer, AlbumNormalizer
from .deromanizer import DeRomanizer
from .meta import MetaAudio
from .catalog import catalog_id

QUERY_TEMPLATE = "https://itunes.apple.com/search?term=%s&media=music&entity=%s"
LOOKUP_TEMPLATE = "https://itunes.apple.com/lookup?id=%s&entity=album"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/96.0.4664.55 Safari/537.36"
THROTTLED_HTTP_CODES = [403, 429]

class RateLimitError(Exception):
    """The server rejected three consecutive requests; stop this run."""

class AppleDownloader(object):
    def __init__(self, verbose: bool, throttle: float, art_size: int, art_quality: int, request_interval: float = 10):
        quality_suffix = "bb" if art_quality == 0 else f"-{art_quality}"
        self.file_suffix = f"{art_size}x{art_size}{quality_suffix}"
        self.verbose = verbose
        self.throttle = throttle
        self.request_interval = request_interval
        self._last_request = None
        self._rate_limits = 0
        self.strict_matching = False
        self.album_title_fallback = False
        self.diagnostics = {}
        self.lookup_cache = {}
        self.track_id_fallback = False
        self.artist_normalizer = ArtistNormalizer()
        self.album_normalizer = AlbumNormalizer()
        self.deromanizer = DeRomanizer()
        
    def _urlopen_safe(self, url: str) -> str:
        while True:
            if self._last_request is not None:
                delay = self.request_interval - (time.monotonic() - self._last_request)
                if delay > 0:
                    if self.verbose:
                        print(f"Waiting {delay:.1f} seconds before the next request...", flush=True)
                    time.sleep(delay)
            try:
                q = Request(url)
                q.add_header("User-Agent", USER_AGENT)
                self._last_request = time.monotonic()
                with urlopen(q) as response:
                    data = response.read()
                self._rate_limits = 0
                return data
            except HTTPError as e:
                if e.code in THROTTLED_HTTP_CODES:
                    self._rate_limits += 1
                    if self._rate_limits >= 3:
                        raise RateLimitError("Stopped after three consecutive rate-limit responses") from e
                    delay = self.throttle * (2 ** (self._rate_limits - 1))
                    retry_after = e.headers.get('Retry-After') if e.headers else None
                    if retry_after:
                        try:
                            retry_delay = float(retry_after)
                        except ValueError:
                            try:
                                retry_delay = (parsedate_to_datetime(retry_after) - datetime.now(timezone.utc)).total_seconds()
                            except (TypeError, ValueError, OverflowError):
                                retry_delay = 0
                        delay = max(delay, retry_delay)
                    domain = urlparse(url).netloc
                    print(f"WARNING: Request limit exceeded from {domain} ({self._rate_limits}/3), trying again in {delay} seconds...")
                    time.sleep(delay)
                else:
                    raise e

    def _urlopen_text(self, url: str) -> str:
        try:
            return self._urlopen_safe(url).decode("utf8")
        except Exception as error:
            if ("certificate verify failed" in str(error)):
                print(f"ERROR: Python doesn't have SSL certificates installed, can't access {url}")
                print("Please run 'Install Certificates.command' from your Python installation directory.")
            else:
                print(f"ERROR: reading URL ({url}): {str(error)})")
            raise

    def _download_from_url(self, image_url: str, dest_path: str):
        image_data = self._urlopen_safe(image_url)
        if self.verbose:
            print(f"Downloading from: {image_url}")
        temporary_path = dest_path + '.part'
        try:
            with open(temporary_path, 'wb') as file:
                file.write(image_data)
            os.replace(temporary_path, dest_path)
        finally:
            if os.path.exists(temporary_path):
                os.remove(temporary_path)
        print(f"Downloaded cover art: {dest_path}")

    def _query(self, artist: str, album: str, title: str) -> dict:
        token = album or title
        entity = "album" if album else "musicTrack"
        query_term = f"{artist} {token}"
        if token in artist:
            query_term = artist
        elif artist in token:
            query_term = token
        url = QUERY_TEMPLATE % (quote(query_term), entity)
        self.diagnostics.setdefault('queries', []).append(url)
        if self.verbose:
            print(f"Searching Apple: {query_term} ({entity})", flush=True)
        raw_json = self._urlopen_text(url)
        if raw_json:
            info = json.loads(raw_json)
            if isinstance(info, dict) and isinstance(info.get('resultCount'), int) and isinstance(info.get('results'), list):
                return info
        raise ValueError("Invalid Apple search response")

    def _get_data(self, meta: MetaAudio) -> Tuple[str, str, dict, bool]:
        artist = self.artist_normalizer.normalize(meta.artist)
        album = self.album_normalizer.normalize(meta.album)
        title = self.album_normalizer.normalize(meta.title)
        info = self._query(artist, album, title)
        if not info or not info.get('resultCount'):
            # no result found, try replacing any roman numerals
            converted_artist = self.deromanizer.convert_all(artist)
            converted_album = self.deromanizer.convert_all(album)
            if not self.strict_matching or (artist, album) != (converted_artist, converted_album):
                artist, album = converted_artist, converted_album
                info = self._query(artist, album, title)
        return (artist, album, info, len(album) == 0)

    def download(self, meta: MetaAudio, art_path: str) -> bool:
        self.diagnostics = {'queries': [], 'candidates': [], 'reason': '', 'status': 'no_match'}
        collection_id = catalog_id(getattr(meta, 'effective_collection_id', None)) or catalog_id(getattr(meta, 'collection_id', None))
        track_id = catalog_id(getattr(meta, 'track_id', None)) if self.track_id_fallback else None
        candidate = None
        if collection_id:
            self.diagnostics['lookup_method'] = 'collection_id'
            candidate = self._lookup_collection(collection_id)
            self.diagnostics['lookup_outcome'] = 'resolved' if candidate else 'collection unavailable'
        if not candidate and track_id:
            self.diagnostics['lookup_method'] = 'track_id'
            associated = self._lookup_track_collection(track_id)
            candidate = self._lookup_collection(associated) if associated else None
            self.diagnostics['lookup_outcome'] = 'resolved' if candidate else 'track/associated collection unavailable'
        if candidate:
            self._record_candidate(candidate, 'verified purchased catalog ID')
            self.diagnostics['resolved_collection_id'] = str(candidate['collectionId'])
            if self.diagnostics['lookup_method'] == 'track_id':
                stem, extension = os.path.splitext(art_path)
                art_path = stem + '.track-' + track_id + extension
            self.diagnostics['downloaded_path'] = art_path
            self._download_from_url(candidate['artworkUrl100'].replace('100x100bb', self.file_suffix), art_path)
            self.diagnostics.update(status='matched', reason='verified purchased catalog ID')
            return True
        if getattr(meta, 'catalog_conflict', False) is True:
            self.diagnostics.update(status='manual_review', reason='conflicting folder IDs: catalog lookup unavailable; no text cover applied')
            return False
        self.diagnostics['lookup_method'] = 'title_artist_search'
        self.diagnostics.setdefault('lookup_outcome', 'identifiers missing')
        if collection_id or track_id:
            stem, extension = os.path.splitext(art_path)
            art_path = stem + '.search' + extension
        self.diagnostics['downloaded_path'] = art_path
        (meta_artist, meta_album, info, title_only) = self._get_data(meta)
        if self.strict_matching:
            matches = {}
            for candidate in info['results']:
                artist = self.artist_normalizer.normalize(candidate.get('artistName'))
                album = self.album_normalizer.normalize(candidate.get('collectionName'))
                reason = ('missing artist metadata' if not meta_artist else
                          'artist mismatch' if meta_artist != artist else
                          'missing album metadata' if not meta_album else
                          'album title is not an exact normalized match' if meta_album != album else
                          'recording or edition wording differs' if self._recording_title(meta.album) != self._recording_title(candidate.get('collectionName', '')) else
                          'missing artwork URL' if not candidate.get('artworkUrl100') else '')
                self._record_candidate(candidate, reason or 'exact normalized match')
                if not reason:
                    key = candidate.get('collectionId') or candidate['artworkUrl100']
                    matches[key] = candidate
            if len(matches) == 1:
                candidate = next(iter(matches.values()))
                self._download_from_url(candidate['artworkUrl100'].replace('100x100bb', self.file_suffix), art_path)
                self.diagnostics.update(status='matched', reason='one exact normalized album match')
                self.diagnostics['lookup_outcome'] += '; text match'
                return True
            if len(matches) > 1:
                self.diagnostics.update(status='manual_review', reason='multiple catalog albums match; no cover accepted')
                for detail in self.diagnostics['candidates']:
                    if detail['reason'] == 'exact normalized match':
                        detail['reason'] = 'ambiguous: multiple distinct catalog albums match'
            else:
                self.diagnostics['reason'] = 'no candidate passed artist and exact album checks'
                if self.album_title_fallback and meta_album:
                    fallback = self._query('', meta_album, '')
                    for candidate in fallback['results']:
                        self._record_candidate(candidate, 'album-title fallback: manual review required')
                    if fallback['results']:
                        self.diagnostics.update(status='manual_review', reason='album-title candidates require manual review')
            self.diagnostics['lookup_outcome'] += '; ' + self.diagnostics['status']
            return False
        if info:
            try:
                art = ""
                # go through albums, use exact match or first contains match if no exacts found
                results = reversed(info.get('results'))
                if title_only:
                    # if no album name provided, use earliest matching release
                    results = reversed(sorted(results, key=lambda x: x.get('releaseDate')))
                for album_info in results:
                    artist = self.artist_normalizer.normalize(album_info.get('artistName'))
                    album = self.album_normalizer.normalize(album_info.get('collectionName'))
                    
                    if not meta_artist in artist:
                        continue
                    if not title_only and not meta_album in album:
                        continue
                    
                    art = album_info.get('artworkUrl100').replace('100x100bb', self.file_suffix)
                    if not title_only and meta_album == album:
                        break # exact match found
                if art:
                    self._download_from_url(art, art_path)
                    self.diagnostics['lookup_outcome'] += '; text match'
                    return True
            except Exception as error:
                print(f"ERROR encountered when downloading for artist ({meta_artist}) and album ({meta_album})")
                print(error)
                raise

        if self.verbose:
            print(f"Failed to find matching artist ({meta_artist}) and album ({meta_album})")
        return False

    def _record_candidate(self, candidate, reason):
        detail = {key: candidate.get(key) for key in
                  ('collectionId', 'collectionName', 'artistName', 'collectionViewUrl', 'artworkUrl100')}
        detail['reason'] = reason
        detail['query'] = (self.diagnostics.get('queries') or [''])[-1]
        self.diagnostics['candidates'].append(detail)
        if self.verbose and not self.strict_matching:
            print(f"Candidate: {detail['collectionName']} / {detail['artistName']}: {reason}")

    @staticmethod
    def _recording_title(title):
        # Keep all edition, cast, and disc wording; only normalize Unicode/case/spacing.
        return ' '.join(unicodedata.normalize('NFKC', title or '').casefold().split())

    def download_approved(self, approval, art_path):
        self.diagnostics = {'queries': [], 'candidates': [], 'status': 'approved', 'reason': 'explicit manual approval'}
        self.diagnostics.update(lookup_method='manual_approval', lookup_outcome='approved')
        url = approval.get('artwork_url')
        if approval.get('collection_id'):
            candidate = self._lookup_collection(approval['collection_id'])
            if not candidate:
                raise ValueError('Approved collection ID did not resolve to exactly one album with artwork')
            self._record_candidate(candidate, 'explicitly approved collection ID')
            url = candidate['artworkUrl100'].replace('100x100bb', self.file_suffix)
        self._download_from_url(url, art_path)
        return True

    def _lookup(self, identity, entity):
        key = (entity, identity)
        url = LOOKUP_TEMPLATE % identity if entity == 'album' else 'https://itunes.apple.com/lookup?id=' + identity
        self.diagnostics.setdefault('queries', []).append(url)
        if key in self.lookup_cache:
            result = self.lookup_cache[key]
            if isinstance(result, Exception):
                raise result
            return result
        if self.verbose:
            print(f'Looking up Apple {entity} ID: {identity}', flush=True)
        try:
            info = json.loads(self._urlopen_text(url))
            if not isinstance(info, dict) or not isinstance(info.get('results'), list) or not isinstance(info.get('resultCount'), int):
                raise ValueError('Invalid Apple lookup response')
        except Exception as error:
            # Suppress duplicate failing lookups during this run, never persist them.
            self.lookup_cache[key] = error
            raise
        self.lookup_cache[key] = info
        return info

    def _lookup_collection(self, identity):
        candidates = [item for item in self._lookup(identity, 'album')['results']
                      if item.get('wrapperType') == 'collection' and catalog_id(item.get('collectionId')) == identity
                      and item.get('artworkUrl100')]
        return candidates[0] if len(candidates) == 1 else None

    def _lookup_track_collection(self, identity):
        candidates = [item for item in self._lookup(identity, 'track')['results']
                      if item.get('wrapperType') == 'track' and catalog_id(item.get('trackId')) == identity
                      and catalog_id(item.get('collectionId'))]
        return catalog_id(candidates[0]['collectionId']) if len(candidates) == 1 else None
