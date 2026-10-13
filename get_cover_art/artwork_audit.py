"""Read-only inspection independent of the embedding metadata constructors."""
import base64
import csv
import io
import os
from collections import defaultdict

from mutagen import File
from mutagen.flac import Picture
from PIL import Image
from .meta import EXT_TO_CONSTRUCTOR


def audio_paths(path):
    if os.path.isfile(path):
        paths = [path]
    else:
        paths = (os.path.join(root, name) for root, dirs, files in os.walk(path)
                 for name in sorted(files))
    for filename in paths:
        if os.path.splitext(filename)[1][1:].lower() in EXT_TO_CONSTRUCTOR:
            yield filename


def validate_image(data):
    with Image.open(io.BytesIO(data)) as image:
        image.verify()
    with Image.open(io.BytesIO(data)) as image:
        image.load()


def inspect_artwork(path):
    row = dict(path=os.path.abspath(path), folder=os.path.abspath(os.path.dirname(path)),
               album='', album_artist='', track_artist='', title='', status='unreadable', reason='')
    try:
        audio = File(path)
        if audio is None:
            raise ValueError('Unrecognized audio data')
        tags = audio.tags or {}
        def tag(*names):
            for name in names:
                value = tags.get(name)
                if value:
                    if hasattr(value, 'text'):
                        value = value.text
                    return str(value[0])
            return ''
        row.update(album=tag('©alb', 'TALB', 'album'), title=tag('©nam', 'TIT2', 'title'),
                   album_artist=tag('aART', 'TPE2', 'albumartist'),
                   track_artist=tag('©ART', 'TPE1', 'artist'))
        pictures = list(tags.get('covr', []))
        if hasattr(tags, 'getall'):
            pictures.extend(frame.data for frame in tags.getall('APIC'))
        pictures.extend(p.data for p in getattr(audio, 'pictures', []))
        for value in tags.get('metadata_block_picture', []):
            try:
                pictures.append(Picture(base64.b64decode(value, validate=True)).data)
            except Exception as error:
                row.update(status='invalid', reason='Invalid embedded picture block: ' + str(error))
                return row
        row['status'] = 'missing' if not pictures else 'embedded'
        if pictures:
            try:
                for data in pictures:
                    validate_image(data)
            except Exception as error:
                row.update(status='invalid', reason=str(error))
    except Exception as error:
        row.update(status='unreadable', reason=str(error))
    return row


def write_report(rows, destination):
    fields = ['path', 'folder', 'album', 'album_artist', 'track_artist', 'title', 'status',
              'reason', 'folder_total', 'folder_embedded', 'folder_missing', 'folder_invalid',
              'local_cover', 'local_cover_available', 'queries', 'candidates']
    with open(destination, 'w', encoding='utf-8-sig', newline='') as output:
        writer = csv.DictWriter(output, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)


def audit_artwork(path, destination):
    rows = [inspect_artwork(filename) for filename in audio_paths(path)]
    folders = defaultdict(list)
    for row in rows:
        folders[row['folder']].append(row)
    missing_folders = partial_folders = 0
    for group in folders.values():
        embedded = sum(row['status'] == 'embedded' for row in group)
        missing = sum(row['status'] == 'missing' for row in group)
        invalid = len(group) - embedded - missing
        missing_folders += bool(missing)
        partial_folders += bool(embedded and embedded < len(group))
        for row in group:
            row.update(folder_total=len(group), folder_embedded=embedded,
                       folder_missing=missing, folder_invalid=invalid)
    write_report([row for row in rows if row['status'] != 'embedded'], destination)
    totals = {status: sum(row['status'] == status for row in rows)
              for status in ('embedded', 'missing', 'invalid', 'unreadable')}
    print(f"Artwork audit: Total: {len(rows)}, Embedded: {totals['embedded']}, Missing: {totals['missing']}, "
          f"Invalid artwork: {totals['invalid']}, Unreadable files: {totals['unreadable']}")
    print(f"Album folders: {len(folders)}, With missing artwork: {missing_folders}, Partial coverage: {partial_folders}")
    print(f"Affected files report: {destination}")
    return rows
