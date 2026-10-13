"""Loopback-only HTTP adapter. All file access uses server-owned identifiers."""
import io
import os
import secrets
import webbrowser
from urllib.parse import urlparse

from flask import Flask, jsonify, request, send_file, render_template
from .review_library import ReviewLibrary
from .artwork_audit import validate_image


def create_app(library):
    app = Flask(__name__)
    app.config['MAX_CONTENT_LENGTH'] = 12 * 1024 * 1024
    token = secrets.token_hex(32)

    @app.before_request
    def protect():
        host = request.host.split(':')[0]
        if host != '127.0.0.1':
            return jsonify(error='Only the loopback host is allowed'), 403
        if request.method != 'GET':
            origin = request.headers.get('Origin')
            if origin and origin != request.host_url.rstrip('/'):
                return jsonify(error='Cross-origin request rejected'), 403
            if request.headers.get('X-Review-Token') != token:
                return jsonify(error='Invalid review token'), 403
        if request.headers.get('Sec-Fetch-Site') == 'cross-site':
            return jsonify(error='Cross-site request rejected'), 403

    @app.after_request
    def headers(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    @app.errorhandler(ValueError)
    def invalid(error):
        return jsonify(error=str(error)), 400

    @app.errorhandler(413)
    def too_large(error):
        return jsonify(error='Upload exceeds 12 MB'), 413

    @app.errorhandler(Exception)
    def failure(error):
        from werkzeug.exceptions import HTTPException
        if isinstance(error, HTTPException):
            return jsonify(error=error.description), error.code
        return jsonify(error=str(error)), 500

    def body():
        data = request.get_json()
        if not isinstance(data, dict):
            raise ValueError('Expected a JSON object')
        return data

    @app.route('/')
    def index():
        return render_template('review.html', token=token)

    @app.route('/api/library')
    def overview():
        with library.lock:
            return jsonify(library.overview())

    @app.route('/api/group/<gid>')
    def group(gid):
        with library.lock:
            return jsonify(group=library.group(gid), approval=library.state['approvals'].get(gid))

    @app.route('/api/refresh', methods=['POST'])
    def refresh():
        library.scan()
        return jsonify(library.overview())

    @app.route('/api/group/<gid>/decision', methods=['POST'])
    def decision(gid):
        data = body()
        library.approve(gid, data.get('status'), data.get('candidate'), data.get('targets'))
        return jsonify(ok=True)

    @app.route('/api/group/<gid>/upload', methods=['POST'])
    def upload(gid):
        if 'image' not in request.files:
            raise ValueError('Choose an image')
        return jsonify(library.upload(gid, request.files['image'].read()))

    @app.route('/api/group/<gid>/search', methods=['POST'])
    def search(gid):
        data = body()
        return jsonify(library.search(gid, data.get('query'), data.get('kind')))

    @app.route('/api/group/<gid>/fetch/<cid>', methods=['POST'])
    def fetch(gid, cid):
        with library.lock:
            group = library.group(gid)
            library.artwork(group, library.candidate(group, cid), fetch=True)
        return jsonify(ok=True)

    @app.route('/api/group/<gid>/image/<cid>')
    def image(gid, cid):
        with library.lock:
            group = library.group(gid)
            path = library.artwork(group, library.candidate(group, cid))
            with open(path, 'rb') as source:
                data = source.read()
        from PIL import Image
        with Image.open(io.BytesIO(data)) as cover:
            mime = Image.MIME.get(cover.format, 'image/jpeg')
        return send_file(io.BytesIO(data), mimetype=mime)

    @app.route('/api/reference/<tid>')
    def reference(tid):
        from mutagen import File
        from mutagen.flac import Picture
        import base64
        with library.lock:
            track = library.tracks.get(tid)
            if not track or not library.allowed(track['path']):
                raise ValueError('Unknown track')
            audio = File(track['path'])
            tags = audio.tags or {}
            pictures = list(tags.get('covr', []))
            if hasattr(tags, 'getall'):
                pictures.extend(frame.data for frame in tags.getall('APIC'))
            pictures.extend(p.data for p in getattr(audio, 'pictures', []))
            pictures.extend(Picture(base64.b64decode(p)).data for p in tags.get('metadata_block_picture', []))
            if not pictures:
                raise ValueError('No embedded cover')
            data = bytes(pictures[0])
            validate_image(data)
        from PIL import Image
        with Image.open(io.BytesIO(data)) as embedded:
            mime = Image.MIME.get(embedded.format, 'image/jpeg')
        return send_file(io.BytesIO(data), mimetype=mime)

    @app.route('/api/apply', methods=['POST'])
    def apply():
        data = body()
        if not isinstance(data.get('dry_run', True), bool) or not isinstance(data.get('replace', False), bool):
            raise ValueError('dry_run and replace must be booleans')
        library.start_apply(data.get('groups'), data.get('dry_run', True), data.get('replace', False))
        return jsonify(ok=True)

    @app.route('/api/job')
    def job():
        with library.lock:
            return jsonify(library.job)

    return app


def launch(args):
    from .review_lock import ReviewInstance
    with ReviewInstance(args.path):
        _serve(args)


def _serve(args):
    from werkzeug.serving import make_server
    from .apple_downloader import AppleDownloader
    files = [os.path.abspath(p) for p in (args.report, args.candidate_report) if p]
    library = ReviewLibrary(args.path, args.reports_dir, args.art_dest or '_cover_art', files or None,
                            AppleDownloader(args.verbose, args.throttle, int(args.art_size), int(args.art_quality), args.request_interval))
    if args.no_download:
        library.downloader = None
    server = make_server('127.0.0.1', args.port, create_app(library), threaded=True)
    url = 'http://127.0.0.1:{}/'.format(server.server_port)
    print('Artwork review: ' + url, flush=True)
    print('Ctrl+C stops the server. Launching does not download or embed artwork.', flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('Stopping artwork review; any active write will finish safely.', flush=True)
    finally:
        server.server_close()
        if library.worker:
            library.worker.join()
