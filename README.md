# get_cover_art

### THE PROBLEM
Missing cover art for large imported music libraries.  

### EXISTING SOLUTIONS

1. Apple's Music App (and its predecessor iTunes) has a "Get Album Artwork" feature, but it isn't reliable and doesn't actually embed cover art into your audio files.  That means if you move your music library elsewhere, you'll be missing all your album artwork.

2. Metadata utilities like Metadatics are great (and cheap if not free), but they can require a lot of manual interaction to go through each album and select artwork from search results.  This can be forbidding for large libraries of thousands of albums.

### THIS SOLUTION
This Python package will batch-update your entire library without manual interaction for each album.

It uses Apple Music's artwork, which is already standardized and high-quality.  It also embeds the artwork directly into your audio files, so that it's independent of your player.

## Supported formats (so far)
- MP3
- MP4 (.m4a, .m4b)
- FLAC
- DSF
- Ogg Vorbis
- Opus
- WAV*

(* NOTE: Apple Music and other players may not recognize ID3 tags on WAV files.)

## Setup

### Requirements
- Python 3.5 or greater
- Python packages: [mutagen](https://pypi.org/project/mutagen/)
  - automatically installed if you use `pip install` below

### Installation

You can download this repository from GitHub, or grab it from [PyPI](https://pypi.org/project/get-cover-art/) using pip:

```
$ pip install get_cover_art
```

To upgrade existing installations:
```
$ pip install --upgrade get_cover_art
```

## Usage

### From the Command Line
```
[python -m] get_cover_art [--path=<path_to_audio_file_or_folder>] [--options]

  --path PATH           audio file, or folder of audio files (recursive)

artwork options:
  --art-size ART_SIZE   square dimensions of artwork (default: 500)
  --art-quality ART_QUALITY
                        jpeg compression quality (1-100, default: auto)
  --art-dest DEST       set artwork destination folder
  --art-dest-inline     set artwork destination folder to same folders as audio files
  --art-dest-filename ART_DEST_FILENAME
                        set artwork destination filename format. Accepts
                        {artist}, {album}, {album_or_title}, {filename}, and
                        {title}. Default is '{artist} - {album_or_title}.jpg'
  --external-art-mode {before,after,none}
                        Use images from local disk: "before" prevents
                        downloads, "after" downloads as a fallback. Default is none.
  --external-art-filename EXTERNAL_ART_FILENAME [EXTERNAL_ART_FILENAME ...]
                        Filename(s) of folder art to use. Accepts {artist},
                        {album}, and {title} for replacement: e.g. cover.jpg
                        or {album}-{artist}.jpg ; this does not affect the filename
                        for art that must be fetched (use --art-dest-filename for
                        that).

behavior options:
  --test, --no-embed    scan and download only, don't embed artwork
  --no-download         embed only previously-downloaded artwork
  --clear               clear artwork from audio file (regardless of finding art)
  --cleanup             remove downloaded artwork files afterward
  --force               process files regardless of existing artwork
  --verbose             print verbose logging
  --no-skip             don't skip previously-scanned files
  --throttle THROTTLE   initial rate-limit backoff in seconds (default: 3)
  --request-interval SECONDS
                        minimum seconds between requests (default: 10)

filter options:
  --skip-artwork SKIP_ARTWORK
                        (maintained between runs) file listing destination art files to skip
  --skip-artists SKIP_ARTISTS
                        file listing artists to skip
  --skip-albums SKIP_ALBUMS
                        file listing albums to skip
```
if you omit `path`, it will scan the current working directory

_Pro Tip:_ You can run with `--test` first, then browse/prune the downloaded artwork, then run again with `--no-download` to embed only the artwork you didn't prune.

Requests (including artwork downloads and retries) are paced at least 10 seconds
apart by default. `--request-interval` changes this minimum; `--throttle` keeps
its existing role as the initial delay after a rejection. HTTP 429 and the
existing 403 handling use exponential backoff, with `Retry-After` seconds or an
HTTP date taking precedence when longer. Three consecutive rejections stop the
scan; a successful response resets the count. Wait before rerunning a stopped
scan. Pacing reduces request frequency but cannot guarantee avoiding limits.

Downloaded images are reused across runs and written through a temporary file
so a failed write does not replace an existing cover. Temporary network errors
and invalid responses are reported as failures and are not saved as no matches.
An album that fails temporarily is not requested again during the same scan.
Only completed searches with no matching artwork enter the skip-artwork list.
Existing images are usable even if an older run added them to that list. To retry
old failures without a downloaded image, use `--no-skip`. Unsupported files such
as PDF booklets are ignored silently. The final report includes file totals and
unique artwork destinations downloaded, skipped, or failed.

For a Windows copy, first download and review artwork:

```powershell
python -m get_cover_art --path "C:\Parker\Development\Music library copy\Music" --art-dest "C:\Parker\Development\Apple Music Covers\_cover_art" --test --request-interval 10
```

Rerun that command later if rate limiting stops it; existing covers are reused.
After reviewing the covers, embed them without any network requests:

```powershell
python -m get_cover_art --path "C:\Parker\Development\Music library copy\Music" --art-dest "C:\Parker\Development\Apple Music Covers\_cover_art" --no-download
```

Run the automated tests with `python -m unittest discover -s tests -v` after
installing the package dependencies. Tests mock HTTP and timing and do not
contact Apple.

### From the Python Environment
```python
from get_cover_art import CoverFinder

finder = CoverFinder(OPTIONS)

# then you can run either of these:
finder.scan_folder(PATH_TO_AUDIO_LIBRARY)
finder.scan_file(PATH_TO_AUDIO_FILE)
```

- `OPTIONS` is a dict of the same options listed for the commandline, e.g. `--verbose` -> `{'verbose': True}`
- you can omit `PATH_TO_AUDIO_LIBRARY` to default to your current directory
- your `CoverFinder` object keeps a list of files_processed, files_skipped, files_failed, files_invalid

### Audit and recover missing artwork

"Skipped" is not equivalent to "has artwork": normal scans skip existing
embedded art, artist/album filters, cached unsuccessful searches, and new
searches with no acceptable match. `--no-download` can also skip tracks without
a cached cover. A downloaded JPEG normally counts as processed when reused;
it does not itself cause a skip. The legacy matcher prefers album artist over
track artist, requires it to be contained in the catalog artist, and uses
normalized album-name containment. Cast and soundtrack metadata can therefore
produce no match even when the album is in the catalog. Older cache entries
also do not distinguish a genuine no-match from older temporary failures.
The regular scan's summary does not verify the saved artwork; the audit and
targeted recovery below check actual embedded image data.

Install/update dependencies with `python -m pip install -e .` (Pillow is now
used to validate images). Run this independent, read-only audit first:

```powershell
.\.venv\Scripts\python.exe -m get_cover_art --path "C:\Parker\Development\Music library copy\Music" --audit-artwork --report artwork_audit.csv
```

This makes no requests and changes no audio, downloaded artwork, or skip lists.
The CSV contains affected files, album and artist metadata, status/reason,
and counts for each containing album folder. The summary separates missing
artwork, invalid images, and unreadable audio; partial folders contain both
valid artwork and affected tracks. Files lacking artist/title tags remain
auditable. Unsupported files such as PDF booklets are ignored.

First recover using downloaded covers only, without any Apple requests:

```powershell
.\.venv\Scripts\python.exe -m get_cover_art --path "C:\Parker\Development\Music library copy\Music" --art-dest "C:\Parker\Development\Apple Music Covers\_cover_art" --missing-only --no-download --report artwork_recovery_local.csv
```

Review the report. To permit paced searches for missing covers, including
explicit retries of cached unsuccessful matches, run:

```powershell
.\.venv\Scripts\python.exe -m get_cover_art --path "C:\Parker\Development\Music library copy\Music" --art-dest "C:\Parker\Development\Apple Music Covers\_cover_art" --missing-only --retry-unmatched --request-interval 5 --verbose --report artwork_recovery.csv
```

Omit `--retry-unmatched` to keep unsuccessful-search cache entries in effect.
It only retries destinations needed by currently missing tracks and does not
clear the cache. Add `--test` to download/review without embedding, then rerun
the local-only recovery command to embed approved covers. Existing embedded
art is preserved, including on partially covered albums. Invalid existing art
requires manual review and is not automatically overwritten. Local downloaded
images are validated before embedding; saved audio is checked after embedding.

Recovery CSVs record local-cover availability, actual query URLs, candidates,
and outcome/rejection reasons. Failed requests stay retryable; repeated tracks
sharing an artwork destination do not trigger repeated failed searches within
one run. Recovery retains the pacing, backoff, and three-rejection stop behavior.
After a stop, the report covers only tracks reached; rerun the audit to identify
everything still missing. Recovery reports are also written when interrupted.

Targeted recovery accepts only one catalog album with an exact normalized
artist match and an album title matching Unicode-normalized case and spacing.
Edition, cast, recording, and disc wording is preserved. Multiple distinct matching catalog
albums are reported for manual review without downloading or caching a miss.
For soundtracks/cast albums, add `--album-title-fallback` to explicitly permit
an extra album-title-only search after normal matching fails. Those candidates
are **never automatically accepted**; inspect their names, artists, catalog
links, and artwork URLs in the CSV. Put a manually approved image at the
reported `local_cover` path, then rerun local-only recovery. The default
filename does not include a catalog ID; review reused covers for collisions
or incorrect older matches, since image validation cannot confirm album identity.

`--missing-only` rejects `--force`, `--clear`, `--cleanup`, and `--no-skip` to
preserve existing artwork and the skip cache. Normal scanning remains available
with its previous matching behavior. Re-run the audit after recovery to measure
the remaining gaps; a successful run does not guarantee catalog availability.

### Album summaries, candidate review, and manual approvals

Missing-only scans print one summary for each local album (folder, album title,
and preferred album artist), with affected track count, actual search/lookup
URLs, candidates and reasons, and outcome counts. Summaries appear after each
containing folder, or when its scan stops on rate limiting or is interrupted. Search and
pacing messages still appear during a verbose run. The per-file recovery CSV
remains available; the additional `artwork_candidates.csv` provides separate
rows with `local_folder`, `local_album`, `local_artist`, `track_count`,
`outcome`, `candidate_title`, `candidate_artist`, `artwork_url`, `collection_id`,
`collection_url`, `rejection_reason`, and `query`. Repeated tracks do not
duplicate candidate rows. Set another destination with `--candidate-report`.
No-match searches remain cached; use `--retry-unmatched` to retry them.
Ambiguous matches and title-only fallback candidates require review and do not
become cached no-matches. Similar titles or artist substrings alone are never
accepted automatically in missing-only mode; normal scanning is unchanged.

For example, download conservative matches and collect fallback candidates
without embedding:

```powershell
.\.venv\Scripts\python.exe -m get_cover_art --path "C:\Parker\Development\Music library copy\Music" --art-dest "C:\Parker\Development\Apple Music Covers\_cover_art" --missing-only --retry-unmatched --album-title-fallback --test --request-interval 5 --verbose --candidate-report artwork_candidates.csv
```

Create a user-maintained approval CSV with this header:

```csv
local_folder,local_album,local_artist,collection_id,artwork_url,local_artwork
```

Identify each local album with either its `local_folder` or both `local_album`
and `local_artist` (copy these values from the candidate report). Optional
additional identity columns narrow the match; album and artist strings must
match the local metadata exactly. Specify exactly **one** approval target per
row: the reviewed Apple `collection_id`, an HTTPS `artwork_url`, or a
`local_artwork` image path. Relative folder/image paths resolve against the
approval CSV's directory. Conflicting matches are reported as failures.

Example using an already reviewed local image, fully offline:

```csv
local_folder,local_album,local_artist,collection_id,artwork_url,local_artwork
C:\Parker\Development\Music library copy\Music\Reviewed Cast Album,,,,,C:\Parker\Development\Apple Music Covers\approved-cast-cover.jpg
```

To download approved IDs/URLs without embedding:

```powershell
.\.venv\Scripts\python.exe -m get_cover_art --path "C:\Parker\Development\Music library copy\Music" --art-dest "C:\Parker\Development\Apple Music Covers\_cover_art" --missing-only --artwork-overrides artwork_approvals.csv --test --request-interval 5 --verbose
```

Approvals bypass unsuccessful-search cache entries but respect artist/album
filters and existing embedded artwork. IDs use a lookup for that exact album;
approved URLs download directly without a search. Both use the existing pacing,
backoff, and three-rejection stop. Downloaded approval files include an Apple
ID or URL digest in their filenames, keeping them separate from old default
covers. Changing an approval therefore cannot silently reuse a different
edition's default cover. Keep the approval CSV when applying these images.

Apply approved images and ordinary downloaded covers with no requests:

```powershell
.\.venv\Scripts\python.exe -m get_cover_art --path "C:\Parker\Development\Music library copy\Music" --art-dest "C:\Parker\Development\Apple Music Covers\_cover_art" --missing-only --artwork-overrides artwork_approvals.csv --no-download
```

An ID or URL approval alone cannot operate offline until its image has been
downloaded; missing images are reported without any lookup or request. For an
existing image under any filename, use `local_artwork`. CSV reports cover only
affected tracks reached during the run; re-audit afterward to verify coverage.

## How it works
1. First, it recursively scans your provided folder for supported files.
  - Step 1 is skipped if you specified a single file instead of a folder.
2. For each file without embedded artwork (or all files if `--force` is used), attempts to download from Apple Music based on artist and album metadata.
  - Step 2 is skipped if it had already downloaded (or attempted to download) the image file.
    - Existing artwork files are reused; confirmed unmatched artwork is cached in `skip_artwork.txt` when that file exists.
    - You can ignore the cached list by using `--no-skip`
  - Step 2 is also skipped based on `--no-download` or other `--skip-*` options.
3. If artwork is found, it's embedded into the audio file.

### Why do you download from Apple Music and not Google image search?
1. Google's Image Search API requires a dev token (so does Apple Music's API, but not its public web query URL).
2. Google search queries are heavily throttled.
3. Apple Music's cover sizes are standardized and sufficiently large.

### Using external art sources

The external-art options allow you to fall back on local folder art.
Some other scraping systems may have created cover art: for instance, some
Kodi scrapers create a "cover.jpg" image in each album directory.

Specifying "--external-art-mode before" will use these existing images, and only
download images if there is no existing image. Specifying "--external-art-mode
after" will attempt to download artwork as usual, only falling back on the
existing images if the download is unable to locate new art.

The "--external-art-filename" option allows you to specify the filename(s) to use
for this existing folder art. It defaults to "cover.jpg \_albumcover.jpg folder.jpg", which are 3 filenames commonly used by other scraping programs. You
can also use bracket formatting with the artist, album, and title fields:
for instance, --external-art-filename "{artist}-{album}-cover.jpg" would create
filenames such as "The Beatles-Abbey Road-cover.jpg".

The "--art-dest-filename" option allows you to specify the filename used to
store downloaded files, for interoperability with other systems. You __must__
be careful to avoid collisions between different albums: The default is
"{artist} - {album_or_title}.jpg". If you know that all of the albums you're running
against have their own directories _and_ you are using "--art-dest-inline", then you
could use something more generic (such as "cover.jpg").  A note about the format:
- {album_or_title} falls back on the title tag if the album tag is blank
- {filename} uses the filename of the source audio file (minus the extension)

_Pro Tip:_ If you have a cover.jpg in each album directory, you can use:
"--external-art-mode before --external-art-filename cover.jpg --art-dest-inline"
and the local images will be used when present (avoiding network lookups).
You could also specify "--art-dest-filename cover.jpg" if you want to store the
newly downloaded covers in a similar location (again, this is only sane
in combination with --art-dest-inline, in cases where each album is stored in 
a separate directory).

## Troubleshooting

### The artwork is embedded now, but Apple's Music App still won't show it.
Try re-importing one of your embedded files.  If the re-imported version shows artwork, you need to reimport your music library.  You can do this without losing your playlists as follows:
1. File->Library->Export Library... and name your exported library file.
2. Visit Music->Preferences...->Files and screenshot your options.  You'll need to restore them later.
3. Quit the app and relaunch while holding down the Option key.
4. Choose "Create Library..." and pick a new location.
5. Visit Music->Preferences...->Files and restore your desired options.
6. File->Library->Import Playlist... and choose your library file from step 1.  (Yes, it's called "Import Playlist..." but you actually use this to import your library.)

Step 6 will take a while.

### The artwork appears in Apple's Music App but not my iOS device.
You'll have to unsync all your music and re-sync it again.  Try it with a single file first.
