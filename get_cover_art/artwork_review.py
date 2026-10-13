"""Album summaries, candidate review, and explicit manual approvals."""
import csv
import hashlib
import json
import os
from collections import Counter
from urllib.parse import urlparse


def album_key(row):
    # Folder is part of the identity: identically named editions stay separate.
    return (os.path.normcase(row['folder']), row['album'], row['album_artist'] or row['track_artist'])


def album_groups(rows):
    groups = {}
    for row in rows:
        groups.setdefault(album_key(row), []).append(row)
    return groups.values()


def details(group, field):
    unique = []
    for row in group:
        for value in json.loads(row.get(field) or '[]'):
            if value not in unique:
                unique.append(value)
    return unique


def print_album_summaries(rows):
    for group in album_groups(rows):
        first = group[0]
        outcomes = ', '.join(f'{status}: {count}' for status, count in Counter(row['status'] for row in group).items())
        print(f"Album: {first['album'] or '(unknown)'} | {first['album_artist'] or first['track_artist']} | "
              f"{len(group)} affected tracks | {first['folder']}", flush=True)
        print('  Queries: ' + ('; '.join(details(group, 'queries')) or 'none (local artwork, cache, or filter)'))
        candidates = details(group, 'candidates')
        print(f'  Candidates: {len(candidates)}')
        methods = list(dict.fromkeys(f"{row.get('lookup_method', 'none')}: {row.get('lookup_outcome', '')}" for row in group))
        print('  Lookup: ' + '; '.join(methods))
        for candidate in candidates:
            print(f"    {candidate.get('collectionName')} | {candidate.get('artistName')} | "
                  f"ID {candidate.get('collectionId')}: {candidate.get('reason')}")
        reasons = list(dict.fromkeys(row['reason'] for row in group if row.get('reason')))
        print(f"  Outcome: {outcomes}; {'; '.join(reasons)}", flush=True)


def write_candidates(rows, destination):
    fields = ['local_folder', 'local_album', 'local_artist', 'track_count', 'outcome',
              'candidate_title', 'candidate_artist', 'artwork_url', 'collection_id',
              'collection_url', 'rejection_reason', 'query']
    with open(destination, 'w', encoding='utf-8-sig', newline='') as output:
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        for group in album_groups(rows):
            if all(row['status'] in ('embedded', 'cover_available') for row in group):
                continue
            first = group[0]
            candidates = details(group, 'candidates')
            for candidate in candidates:
                writer.writerow(dict(local_folder=first['folder'], local_album=first['album'],
                                     local_artist=first['album_artist'] or first['track_artist'],
                                     track_count=len(group), outcome='; '.join(sorted({row['status'] for row in group})),
                                     candidate_title=candidate.get('collectionName'),
                                     candidate_artist=candidate.get('artistName'),
                                     artwork_url=candidate.get('artworkUrl100'),
                                     collection_id=candidate.get('collectionId'),
                                     collection_url=candidate.get('collectionViewUrl'),
                                     rejection_reason=candidate.get('reason'), query=candidate.get('query', '')))


class ManualOverrides:
    def __init__(self, filename=None):
        self.entries = []
        if not filename:
            return
        base = os.path.dirname(os.path.abspath(filename))
        with open(filename, encoding='utf-8-sig', newline='') as source:
            for number, raw in enumerate(csv.DictReader(source), 2):
                row = {key: (value or '').strip() for key, value in raw.items() if key}
                if not any(row.values()):
                    continue
                if not row.get('local_folder') and not (row.get('local_album') and row.get('local_artist')):
                    raise ValueError(f'Override line {number}: specify local_folder or local_album and local_artist')
                targets = [key for key in ('collection_id', 'artwork_url', 'local_artwork') if row.get(key)]
                if len(targets) != 1:
                    raise ValueError(f'Override line {number}: approve exactly one collection_id, artwork_url, or local_artwork')
                if row.get('collection_id'):
                    if not row['collection_id'].isdigit() or int(row['collection_id']) <= 0:
                        raise ValueError(f'Override line {number}: collection_id must be a positive integer')
                    row['collection_id'] = str(int(row['collection_id']))
                if row.get('artwork_url'):
                    parsed = urlparse(row['artwork_url'])
                    if parsed.scheme != 'https' or not parsed.netloc:
                        raise ValueError(f'Override line {number}: artwork_url must be an HTTPS URL')
                for key in ('local_folder', 'local_artwork'):
                    if row.get(key):
                        row[key] = os.path.abspath(os.path.join(base, row[key]))
                self.entries.append(row)

    def find(self, row):
        matches = []
        for entry in self.entries:
            if entry.get('local_folder') and os.path.normcase(entry['local_folder']) != os.path.normcase(row['folder']):
                continue
            if entry.get('local_album') and entry['local_album'] != row['album']:
                continue
            if entry.get('local_artist') and entry['local_artist'] != (row['album_artist'] or row['track_artist']):
                continue
            matches.append(entry)
        if len(matches) > 1:
            raise ValueError('Multiple manual overrides match this local album')
        return matches[0] if matches else None

    @staticmethod
    def cover_path(entry, default_path):
        if entry.get('local_artwork'):
            return entry['local_artwork']
        identity = ('apple-' + entry['collection_id']) if entry.get('collection_id') else (
            'url-' + hashlib.sha256(entry['artwork_url'].encode('utf-8')).hexdigest()[:16])
        stem, extension = os.path.splitext(default_path)
        return stem + '.' + identity + extension
