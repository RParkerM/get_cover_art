import json
import os
import time
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from urllib.request import Request, urlopen, HTTPError
from urllib.parse import urlparse, quote
from typing import Tuple
from .normalizer import ArtistNormalizer, AlbumNormalizer
from .deromanizer import DeRomanizer
from .meta import MetaAudio

QUERY_TEMPLATE = "https://itunes.apple.com/search?term=%s&media=music&entity=%s"
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
        self.artist_normalizer = ArtistNormalizer()
        self.album_normalizer = AlbumNormalizer()
        self.deromanizer = DeRomanizer()
        
    def _urlopen_safe(self, url: str) -> str:
        while True:
            if self._last_request is not None:
                delay = self.request_interval - (time.monotonic() - self._last_request)
                if delay > 0:
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
            artist = self.deromanizer.convert_all(artist)
            album = self.deromanizer.convert_all(album)
            info = self._query(artist, album, title)
        return (artist, album, info, len(album) == 0)

    def download(self, meta: MetaAudio, art_path: str) -> bool:
        (meta_artist, meta_album, info, title_only) = self._get_data(meta)
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
                    return True
            except Exception as error:
                print(f"ERROR encountered when downloading for artist ({meta_artist}) and album ({meta_album})")
                print(error)
                raise

        if self.verbose:
            print(f"Failed to find matching artist ({meta_artist}) and album ({meta_album})")
        return False
