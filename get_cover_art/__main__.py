import argparse, os, math
from .cover_finder import CoverFinder, DEFAULTS

# This script searches apple music for artwork that is missing from your library
# It saves the artwork alongside the audio and embeds the artwork into the meta tags

# By default it will scan from the current working directory, you can override this
# with commandline parameters or arguments passed into scan_folder()

def check_art_size(value: str) -> float:
    ivalue = int(value)
    if ivalue <= 0:
        raise argparse.ArgumentTypeError("art-size must be a positive integer")
    return ivalue

def check_art_quality(value: str) -> float:
    ivalue = int(value)
    if ivalue < 0 or ivalue > 100:
        raise argparse.ArgumentTypeError("art-quality must be between 1 and 100")
    return ivalue

def check_throttle(value: str) -> float:
    fvalue = float(value)
    if not math.isfinite(fvalue) or fvalue < 0:
        raise argparse.ArgumentTypeError("delay must be finite and non-negative")
    return fvalue

def get_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('--path', help="audio file, or folder of audio files (recursive)", default=".")

    parser_art = parser.add_argument_group('artwork options')
    parser_art.add_argument('--art-size', type=check_art_size, help="square dimensions of artwork (default: 500)", default=DEFAULTS.get('art_size'))
    parser_art.add_argument('--art-quality', type=check_art_quality, help="jpeg compression quality (1-100, default: auto)", default=DEFAULTS.get('art_quality'))
    parser_art.add_argument('--art-dest', '--dest', help="set artwork destination folder", default=DEFAULTS.get('cover_art'))
    parser_art.add_argument('--art-dest-inline', '--inline', help="put artwork in same folders as audio files", action='store_true')
    parser_art.add_argument('--art-dest-filename', default=DEFAULTS.get('art_dest_filename'), help="set artwork destination filename format. Accepts {artist}, {album}, {album_or_title}, {filename}, and {title}. Default is '{artist} - {album_or_title}.jpg'")
    parser_art.add_argument('--external-art-mode', choices=['before', 'after', 'none'], default=DEFAULTS.get('external_art_mode'), help='Use image from local folder; "before" prevents downloads, "after" uses as a fallback.  Default is none.')
    parser_art.add_argument('--external-art-filename', default=DEFAULTS.get('external_art_filename'), help="Filename(s) of folder art to use for preexisting external art. Accepts {artist}, {album}, and {title} for replacement: e.g. cover.jpg or {album}-{artist}.jpg ; this does not affect the filename for art that must be fetched (use --art-dest-filename for that).", nargs="+")

    parser_behavior = parser.add_argument_group('behavior options')
    parser_behavior.add_argument('--test', '--no-embed', '--no_embed', help="scan and download only, don't embed artwork", action='store_true')
    parser_behavior.add_argument('--clear', help="clear artwork from audio file (regardless of finding art)", action='store_true')
    parser_behavior.add_argument('--cleanup', help="remove downloaded artwork files afterward", action='store_true')
    parser_behavior.add_argument('--no-download', '--no_download', help="embed only previously-downloaded artwork", action='store_true')
    parser_behavior.add_argument('--force', help="overwrite existing artwork", action='store_true')
    parser_behavior.add_argument('--verbose', help="print verbose logging", action='store_true')
    parser_behavior.add_argument('--no-skip', '--no_skip', help="don't skip previously-scanned files", action='store_true')
    parser_behavior.add_argument('--throttle', type=check_throttle, help="initial rate-limit backoff in seconds (default: 3)", default=DEFAULTS['throttle'])
    parser_behavior.add_argument('--request-interval', type=check_throttle, help="minimum seconds between requests, including artwork downloads (default: 10)", default=DEFAULTS['request_interval'])
    parser_behavior.add_argument('--audit-artwork', action='store_true', help='read-only embedded artwork audit; no requests or audio changes')
    parser_behavior.add_argument('--missing-only', action='store_true', help='recover only tracks without embedded artwork; preserve caches and existing art')
    parser_behavior.add_argument('--retry-unmatched', action='store_true', help='with missing-only, explicitly retry cached unsuccessful matches')
    parser_behavior.add_argument('--album-title-fallback', action='store_true', help='with missing-only, search album title for manual-review candidates only')
    parser_behavior.add_argument('--report', help='CSV destination (default: artwork_audit.csv or artwork_recovery.csv)')

    parser_filters = parser.add_argument_group('filter options')
    parser_filters.add_argument('--skip-artwork', '--skip_artwork', help="(maintained between runs) file listing destination art files to skip", default=DEFAULTS.get('skip_artwork'))
    parser_filters.add_argument('--skip-artists', '--skip_artists', help="file listing artists to skip", default=DEFAULTS.get('skip_artists'))
    parser_filters.add_argument('--skip-albums', '--skip_albums', help="file listing albums to skip", default=DEFAULTS.get('skip_albums'))

    args = parser.parse_args()
    if args.audit_artwork and args.missing_only:
        parser.error('choose audit-artwork or missing-only')
    if (args.retry_unmatched or args.album_title_fallback) and not args.missing_only:
        parser.error('retry-unmatched and album-title-fallback require missing-only')
    if args.missing_only and (args.force or args.clear or args.cleanup or args.no_skip):
        parser.error('missing-only preserves existing art and cache; incompatible with force, clear, cleanup, no-skip')
    return args

def main():
    args = get_args()
    if args.audit_artwork:
        from .artwork_audit import audit_artwork
        audit_artwork(args.path, args.report or 'artwork_audit.csv')
        return
    finder = CoverFinder(vars(args))
    try:
        if os.path.isfile(args.path):
            finder.scan_file(args.path)
        else:
            finder.scan_folder(args.path)
    finally:
        if args.missing_only:
            from .artwork_audit import write_report
            destination = args.report or 'artwork_recovery.csv'
            write_report(finder.recovery_rows, destination)
            print(f'Recovery report: {destination}')
    print()
    num_processed = len(finder.files_processed)
    num_skipped = len(finder.files_skipped)
    num_failed = len(finder.files_failed)
    print(f"Done!  Processed: {num_processed}, Skipped: {num_skipped}, Failed: {num_failed}")
    print(f"Albums: Downloaded: {len(finder.downloads)}, Skipped: {len(finder.albums_skipped)}, Failed: {len(finder.albums_failed)}")
    if finder.stopped:
        print("Stopped due to rate limiting. Downloaded artwork is preserved; rerun later to resume.")
    if not args.cleanup:
        if finder.art_folder_override:
            print(f"Artwork folder: {finder.art_folder_override}")
        else:
            print("Artwork files are alongside audio files.")
    print()

if __name__ == '__main__':
    main()
