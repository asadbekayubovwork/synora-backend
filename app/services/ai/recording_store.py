"""Where the audio bytes live, and nothing about who owns them.

The row in `tts_recordings` is the record; this module is the bucket under it.
It knows about paths, hashes and `fsync`, and it knows nothing about users,
sessions or money — the same split as `tts_client` and `tts_service`, and for
the same reason: a store that also decided who may read a file would have to be
consulted by anything that ever writes one.

## Content-addressed, so a retry costs one file

The key is the sha256 of the bytes, split into two directory levels:
`ab/cd/abcd….wav`. Two consequences, both wanted. A client that synthesises the
same text with the same voice twice stores one file and two rows, which is the
common case on a retry and on a page that re-renders. And a corrupted or
truncated file is detectable, because the name is a claim about the contents
that can be checked.

Two levels rather than one flat directory: ext4 and APFS both degrade on
directories with a few hundred thousand entries, and `ls` on one is unusable
long before that. 256 × 256 buckets puts a million files at ~15 per directory.

## Written to a temporary file, never to memory

The obvious implementation buffers the stream and writes it once. At
`TTS_MAX_CHARACTERS` a wav is tens of megabytes, three concurrent syntheses per
user are allowed, and the buffer is held for the whole relay — so the obvious
implementation is a memory limit nobody wrote down. `Writer` streams to a
temporary file in the same directory tree and renames it into place at the end,
which is also what makes the final path atomic: a reader never sees a partial
file under a name that promises a whole one.

The per-chunk `write` is synchronous and that is deliberate. It is a buffered
write into the page cache — no `fsync`, no `O_DIRECT` — and it sits in the
relay loop where an `await` would add an event-loop round trip per chunk of
audio. Losing a recording to a power cut is an acceptable outcome; adding
latency to every chunk of every stream is not.

## Nothing here may break a stream

Every entry point is written to be called from the audio path, where the
caller's alternative to "recording failed" is "the customer's synthesis
failed". `Writer.write` swallows its own IO errors, marks itself broken and
keeps the relay going; `commit` returns `None` rather than raising. The service
layer logs what this module gives it and carries on.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import tempfile
from pathlib import Path

from app.core.config import settings

logger = logging.getLogger("synora.recordings")

# A key is produced by this module and then stored, handed to a client, and
# handed back. By the time it returns it is untrusted input, so it is matched
# rather than trusted: two hex bytes, two hex bytes, the full digest, a short
# lowercase extension. `..` cannot be spelled in that alphabet, which is the
# point — a key is joined onto a filesystem root.
KEY_PATTERN = re.compile(r"^[0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f]{64}\.[a-z0-9]{1,8}$")

# A whitelist rather than a sanitiser, because this value becomes the tail of a
# filename. The first four are what `/tts/speech` produces; the rest are what
# arrives at `/stt/transcribe`, where the extension comes off a client-supplied
# filename and is therefore untrusted. Anything unrecognised is stored as
# `.bin`, which is honest: nothing on our side decoded the file.
#
# `pcm` is raw samples with no container and is only playable by something that
# already knows the rate. Stored under its own extension anyway — the row
# carries `sample_rate`, and renaming it `.wav` would be a lie a player acts on.
EXTENSIONS = {
    "mp3": "mp3",
    "wav": "wav",
    "opus": "opus",
    "pcm": "pcm",
    "m4a": "m4a",
    "mp4": "mp4",
    "aac": "aac",
    "flac": "flac",
    "ogg": "ogg",
    "oga": "ogg",
    "webm": "webm",
    "amr": "amr",
    "3gp": "3gp",
}

_INCOMING = "incoming"


def root() -> Path:
    """The store's directory. Read per call, because tests move it."""
    return Path(settings.recordings_dir)


def path_for(key: str) -> Path | None:
    """The file a key names, or `None` if the key is not one we could have made."""
    if not KEY_PATTERN.match(key):
        # Not an error worth raising: the only way to get here is a client
        # sending a key we never issued, and the answer to that is the same 404
        # as a row that does not exist.
        logger.warning("recording_key_rejected %r", key[:80])
        return None
    return root() / key


def extension_for(audio_format: str) -> str:
    return EXTENSIONS.get(audio_format.lower(), "bin")


class Writer:
    """One recording being written, chunk by chunk, as the audio is relayed.

    Created with `begin()`, fed with `write()`, finished with `commit()` or
    `abort()`. Broken at any point it stays broken and silent: `commit()` on a
    broken writer cleans up and answers `None`.
    """

    def __init__(self, handle, temp_path: Path) -> None:
        self._handle = handle
        self._temp_path = temp_path
        self._digest = hashlib.sha256()
        self.bytes_written = 0
        self.broken = False

    def write(self, chunk: bytes) -> None:
        """One chunk of audio. Never raises; see the module docstring."""
        if self.broken:
            return
        try:
            self._handle.write(chunk)
            self._digest.update(chunk)
            self.bytes_written += len(chunk)
        except Exception:  # noqa: BLE001 - a full disk must not end a synthesis
            logger.warning("recording_write_failed after %d bytes", self.bytes_written,
                           exc_info=True)
            self.broken = True
            self._discard()

    async def commit(self, *, audio_format: str) -> tuple[str, str] | None:
        """Close the file and move it to its content address.

        Returns `(key, sha256)`, or `None` when there is nothing to keep. The
        rename is the only step that makes a file visible under a name that
        promises complete contents, which is why everything before it happens
        to a temporary name.
        """
        if self.broken or not self.bytes_written:
            self._discard()
            return None

        digest = self._digest.hexdigest()
        key = f"{digest[:2]}/{digest[2:4]}/{digest}.{extension_for(audio_format)}"
        try:
            await asyncio.to_thread(self._finish, key)
        except Exception:  # noqa: BLE001 - the row is not worth the stream
            logger.warning("recording_commit_failed key=%s", key, exc_info=True)
            self._discard()
            return None
        return key, digest

    def _finish(self, key: str) -> None:
        self._handle.close()
        target = root() / key
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            # Same bytes already stored — the same text and voice synthesised
            # again. Two rows, one file; the caller gets the key either way.
            self._temp_path.unlink(missing_ok=True)
            return
        # `replace` rather than `rename`: atomic on both platforms, and it does
        # not fail if another synthesis of the same content won the race to
        # this name a microsecond ago. Either file is correct — they are the
        # same bytes, which is what a content address means.
        os.replace(self._temp_path, target)

    def abort(self) -> None:
        self._discard()

    def _discard(self) -> None:
        try:
            self._handle.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            self._temp_path.unlink(missing_ok=True)
        except OSError:
            logger.warning("recording_temp_unlink_failed %s", self._temp_path)


async def begin() -> Writer | None:
    """Open a writer, or `None` if recording is off or the disk will not have it."""
    if not settings.recordings_enabled:
        return None
    try:
        return await asyncio.to_thread(_open_writer)
    except Exception:  # noqa: BLE001 - no writable directory is not a 500
        logger.warning("recording_begin_failed dir=%s", settings.recordings_dir,
                       exc_info=True)
        return None


def _open_writer() -> Writer:
    incoming = root() / _INCOMING
    incoming.mkdir(parents=True, exist_ok=True)
    # In the same directory tree as the destination, because `os.replace`
    # across filesystems is not atomic and `/tmp` is frequently its own mount.
    handle = tempfile.NamedTemporaryFile(  # noqa: SIM115 - closed by the Writer
        dir=incoming, prefix="rec-", suffix=".part", delete=False
    )
    return Writer(handle, Path(handle.name))


async def read(key: str) -> bytes | None:
    """The whole file. Used by tests and by nothing on the request path."""
    path = path_for(key)
    if path is None:
        return None
    try:
        return await asyncio.to_thread(path.read_bytes)
    except OSError:
        return None


async def delete(key: str) -> bool:
    """Remove the file a key names. `True` if it was there.

    Called only once the last row referencing the key has gone — see
    `tts_recording_service.delete`. A file with rows still pointing at it is a
    404 on somebody else's recording.
    """
    path = path_for(key)
    if path is None:
        return False
    try:
        return await asyncio.to_thread(_unlink, path)
    except OSError:
        logger.warning("recording_unlink_failed key=%s", key, exc_info=True)
        return False


def _unlink(path: Path) -> bool:
    if not path.exists():
        return False
    path.unlink()
    return True
