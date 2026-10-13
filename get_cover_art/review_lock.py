"""An OS-owned advisory lock prevents two review servers for one library."""
import hashlib
import os
import tempfile


class ReviewInstance:
    def __init__(self, library, directory=None):
        key = hashlib.sha256(os.path.normcase(os.path.realpath(library)).encode('utf-8')).hexdigest()
        self.path = os.path.join(directory or tempfile.gettempdir(), 'get-cover-art-review-' + key + '.lock')
        self.file = None

    def __enter__(self):
        self.file = open(self.path, 'a+b')
        self.file.seek(0, os.SEEK_END)
        if not self.file.tell():
            self.file.write(b'0')
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            self.file = None
            raise ValueError('Another artwork review server is already using this library')
        return self

    def __exit__(self, *args):
        if self.file:
            # Closing releases the OS lock even after a crash. Keep the tiny lock
            # file: removing it could let two processes lock different inodes.
            self.file.close()
            self.file = None
