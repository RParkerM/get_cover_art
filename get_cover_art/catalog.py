"""Purchased catalog identifiers and conservative folder assignments."""
import os


def catalog_id(value):
    if isinstance(value, (list, tuple)):
        value = value[0] if len(value) == 1 else None
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    try:
        number = int(value)
        return str(number) if number > 0 else None
    except ValueError:
        return None


def folder_catalog_plan(rows):
    ids = {row.get('collection_id') for row in rows if row.get('collection_id')}
    metadata = {(row.get('album', '').strip().casefold(),
                 (row.get('album_artist') or row.get('track_artist') or '').strip().casefold()) for row in rows}
    consistent = len(metadata) == 1 and all(next(iter(metadata)))
    conflict = len(ids) > 1
    common = next(iter(ids)) if len(ids) == 1 else None
    plan = {}
    for row in rows:
        own = row.get('collection_id')
        inferred = common if not own and consistent else None
        unresolved = (not own and bool(ids) and not inferred)
        plan[os.path.abspath(row['path'])] = dict(
            collection_id=own or inferred, track_id=row.get('track_id'),
            id_conflict=('unassigned track in folder with conflicting collection IDs' if conflict and not own else
                         'cannot inherit collection ID: album metadata is inconsistent' if unresolved else
                         'multiple collection IDs: verified separately' if conflict else ''),
            unresolved=unresolved, separate=conflict, inferred=bool(inferred))
    return plan


def catalog_cover_path(default_path, collection_id=None, track_id=None):
    stem, extension = os.path.splitext(default_path)
    identity = 'apple-' + collection_id if collection_id else 'track-' + track_id
    return stem + '.' + identity + extension
