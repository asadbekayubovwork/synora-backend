"""Kept syntheses: the row, the file, and the four ways they must not diverge.

Upstream is faked at the transport, as in `test_tts_api.py`, because the thing
under test is what happens to bytes on their way through the relay — and the
relay is what writes the file.

The four properties these exist to hold down:

* **the file is the audio**, byte for byte, and its name is the sha256 of its
  own contents, so a truncated write cannot pass as a whole recording;
* **one file per distinct audio**, because a retry that stored a second copy
  would double the disk for every client that retries;
* **deleting a recording never empties somebody else's**, which is the failure
  content addressing invites and the reason `delete` counts rows first;
* **nothing here can break a synthesis** — a disk that refuses writes costs a
  recording and neither the audio nor the charge.

`RECORDINGS_DIR` is pointed at a tmp_path per test. The default is
`data/recordings`, and a suite that wrote there would leave the developer's own
recordings directory full of `a` characters.
"""

from __future__ import annotations

import hashlib

import httpx
import pytest
from sqlalchemy import select

from app.core.config import settings
from app.models.tts_recording import TtsRecording
from app.models.user import User
from app.services.ai import recording_store, tts_client
from tests.conftest import auth, fund, register_and_verify

TEXT = "a" * 1_000
PRICE_MICROS = 250_000

# Three chunks, so a writer that only ever saw the first would store a file
# whose digest does not match the one the row claims.
AUDIO_CHUNKS = (b"RIFFfake-header", b"frame-one-frame-two", b"frame-three")
AUDIO = b"".join(AUDIO_CHUNKS)
AUDIO_SHA = hashlib.sha256(AUDIO).hexdigest()


class FakeSpeechBox:
    def __init__(self) -> None:
        self.chunks: tuple[bytes, ...] = AUDIO_CHUNKS
        self.status = 200

    async def handle(self, request: httpx.Request) -> httpx.Response:
        if self.status != 200:
            return httpx.Response(self.status, json={"detail": "no"})
        return httpx.Response(
            200, headers={"content-type": "audio/wav"}, content=self._audio()
        )

    async def _audio(self):
        for chunk in self.chunks:
            yield chunk


@pytest.fixture
async def speech_box(monkeypatch, tmp_path):
    box = FakeSpeechBox()
    monkeypatch.setattr(settings, "tts_base_url", "http://speech.test")
    monkeypatch.setattr(settings, "tts_api_key", "test-key")
    monkeypatch.setattr(settings, "recordings_enabled", True)
    monkeypatch.setattr(settings, "recordings_dir", str(tmp_path / "recordings"))
    monkeypatch.setattr(
        tts_client,
        "build_client",
        lambda: httpx.AsyncClient(
            base_url="http://speech.test",
            transport=httpx.MockTransport(box.handle),
        ),
    )
    await tts_client.aclose_client()
    yield box
    await tts_client.aclose_client()


async def funded(client, session, email: str) -> str:
    """A verified account with credit for a few calls. Returns its token."""
    tokens = await register_and_verify(client, email=email)
    user = (
        await session.execute(select(User).where(User.email == email))
    ).scalar_one()
    await fund(session, user.id, paid=20 * PRICE_MICROS)
    await session.commit()
    return tokens["access_token"]


async def synthesize(client, token: str, *, text: str = TEXT, key: str | None = None):
    headers = auth(token)
    if key:
        headers["Idempotency-Key"] = key
    return await client.post(
        "/tts/speech", json={"text": text, "audio_format": "wav"}, headers=headers
    )


# --- the row and the file ---------------------------------------------------


async def test_a_synthesis_is_kept_with_the_text_that_produced_it(
    client, session, price_book, speech_box
):
    token = await funded(client, session, "keeper@example.com")

    response = await synthesize(client, token)
    assert response.status_code == 200

    listing = await client.get("/tts/recordings", headers=auth(token))
    assert listing.status_code == 200
    rows = listing.json()["recordings"]
    assert len(rows) == 1

    row = rows[0]
    # The text, which is the one thing `ai_sessions` deliberately does not keep
    # — it stores a digest, and a digest cannot be read back.
    assert row["text"] == TEXT
    assert row["characters"] == 1_000
    assert row["audio_format"] == "wav"
    assert row["audio_bytes"] == len(AUDIO)
    assert row["sha256"] == AUDIO_SHA
    # The thread back to the money: the same session id the ledger reports.
    assert row["ai_session_id"] == response.headers["X-Synora-Session-Id"]


async def test_the_stored_file_is_the_audio_that_was_delivered(
    client, session, price_book, speech_box
):
    token = await funded(client, session, "bytes@example.com")

    response = await synthesize(client, token)
    assert response.content == AUDIO

    recording_id = (await client.get("/tts/recordings", headers=auth(token))).json()[
        "recordings"
    ][0]["id"]
    played = await client.get(f"/tts/recordings/{recording_id}/audio", headers=auth(token))

    assert played.status_code == 200
    assert played.content == AUDIO
    # The name is a claim about the contents, so it is checkable.
    assert hashlib.sha256(played.content).hexdigest() == AUDIO_SHA


async def test_the_key_is_the_digest_so_the_same_audio_is_stored_once(
    client, session, price_book, speech_box
):
    token = await funded(client, session, "dedup@example.com")

    # Two separate calls, no idempotency key: two charges, two rows, and the
    # identical bytes in both.
    assert (await synthesize(client, token)).status_code == 200
    assert (await synthesize(client, token)).status_code == 200

    rows = (await session.execute(select(TtsRecording))).scalars().all()
    assert len(rows) == 2
    assert rows[0].storage_key == rows[1].storage_key

    stored = list((recording_store.root()).rglob("*.wav"))
    assert len(stored) == 1, stored
    assert stored[0].read_bytes() == AUDIO


async def test_nothing_is_left_behind_in_the_incoming_directory(
    client, session, price_book, speech_box
):
    """The temporary file is renamed into place, or removed. Never both left."""
    token = await funded(client, session, "tidy@example.com")
    await synthesize(client, token)

    incoming = recording_store.root() / "incoming"
    assert list(incoming.iterdir()) == []


# --- what is deliberately not kept ------------------------------------------


async def test_a_call_upstream_refused_leaves_no_recording(
    client, session, price_book, speech_box
):
    token = await funded(client, session, "refused@example.com")
    speech_box.status = 500

    assert (await synthesize(client, token)).status_code == 502

    assert (await client.get("/tts/recordings", headers=auth(token))).json()[
        "recordings"
    ] == []
    # ...and no orphan file either: the writer is aborted, not committed.
    assert list(recording_store.root().rglob("*.wav")) == []


async def test_a_replay_does_not_add_a_second_row_for_the_same_audio(
    client, session, price_book, speech_box
):
    """A retry under a spent key is the original's recording, not a new one."""
    token = await funded(client, session, "replay@example.com")

    first = await synthesize(client, token, key="one-key")
    assert first.status_code == 200
    second = await synthesize(client, token, key="one-key")

    rows = (await client.get("/tts/recordings", headers=auth(token))).json()["recordings"]
    assert len(rows) == 1, second.status_code


async def test_recording_can_be_switched_off_entirely(
    client, session, price_book, speech_box, monkeypatch
):
    monkeypatch.setattr(settings, "recordings_enabled", False)
    token = await funded(client, session, "optout@example.com")

    response = await synthesize(client, token)

    # The synthesis is unaffected; only the keepsake is gone.
    assert response.status_code == 200
    assert response.content == AUDIO
    assert (await client.get("/tts/recordings", headers=auth(token))).json()[
        "recordings"
    ] == []


# --- deleting ---------------------------------------------------------------


async def test_deleting_removes_the_row_and_the_file(
    client, session, price_book, speech_box
):
    token = await funded(client, session, "eraser@example.com")
    await synthesize(client, token)
    recording_id = (await client.get("/tts/recordings", headers=auth(token))).json()[
        "recordings"
    ][0]["id"]

    assert (
        await client.delete(f"/tts/recordings/{recording_id}", headers=auth(token))
    ).status_code == 200

    assert (await client.get("/tts/recordings", headers=auth(token))).json()[
        "recordings"
    ] == []
    assert list(recording_store.root().rglob("*.wav")) == []
    # The charge is untouched: what was said is erased, not that it was paid for.
    usage = (await client.get("/usage", headers=auth(token))).json()
    assert usage["lines"][0]["quantity"] == 1_000


async def test_one_delete_never_empties_another_accounts_playback(
    client, session, price_book, speech_box
):
    """The failure content addressing invites, and the reason `delete` counts."""
    ali = await funded(client, session, "ali@example.com")
    vali = await funded(client, session, "vali@example.com")

    await synthesize(client, ali)
    await synthesize(client, vali)

    ali_row = (await client.get("/tts/recordings", headers=auth(ali))).json()["recordings"][0]
    vali_row = (await client.get("/tts/recordings", headers=auth(vali))).json()["recordings"][0]
    # Same text, same voice, same settings: one file, two owners.
    assert ali_row["sha256"] == vali_row["sha256"]

    await client.delete(f"/tts/recordings/{ali_row['id']}", headers=auth(ali))

    played = await client.get(
        f"/tts/recordings/{vali_row['id']}/audio", headers=auth(vali)
    )
    assert played.status_code == 200
    assert played.content == AUDIO


async def test_another_accounts_recording_is_a_404_rather_than_a_403(
    client, session, price_book, speech_box
):
    ali = await funded(client, session, "ali@example.com")
    vali = await funded(client, session, "vali@example.com")
    await synthesize(client, ali)
    ali_row = (await client.get("/tts/recordings", headers=auth(ali))).json()["recordings"][0]

    for method, path in (
        ("get", f"/tts/recordings/{ali_row['id']}"),
        ("get", f"/tts/recordings/{ali_row['id']}/audio"),
        ("delete", f"/tts/recordings/{ali_row['id']}"),
    ):
        response = await getattr(client, method)(path, headers=auth(vali))
        # 403 would confirm the id exists, which is what a stranger walking the
        # id space is trying to learn.
        assert response.status_code == 404, (method, path)
        assert response.json()["code"] == "recording_not_found"


# --- failure is never the customer's problem --------------------------------


async def test_a_disk_that_refuses_to_write_costs_the_recording_and_nothing_else(
    client, session, price_book, speech_box, monkeypatch
):
    """A full disk mid-relay, which is where it would actually happen.

    The failure is injected at the file handle rather than at `Writer.write`,
    because `Writer.write` is the guard. Patching over it would test a
    hypothetical writer with no error handling and prove nothing about this
    one — and this test exists precisely so that deleting that guard fails
    here rather than in production, halfway through somebody's audio.
    """
    token = await funded(client, session, "fulldisk@example.com")

    class FullDisk:
        def write(self, chunk: bytes) -> None:
            raise OSError("No space left on device")

        def close(self) -> None:
            pass

    def broken_writer() -> recording_store.Writer:
        temp = recording_store.root() / "incoming" / "doomed.part"
        temp.parent.mkdir(parents=True, exist_ok=True)
        return recording_store.Writer(FullDisk(), temp)

    monkeypatch.setattr(recording_store, "_open_writer", broken_writer)

    response = await synthesize(client, token)

    assert response.status_code == 200
    assert response.content == AUDIO
    assert (await client.get("/tts/recordings", headers=auth(token))).json()[
        "recordings"
    ] == []
    balance = (await client.get("/wallet", headers=auth(token))).json()
    assert balance["reserved_micros"] == 0
    assert balance["available_micros"] == 20 * PRICE_MICROS - PRICE_MICROS


async def test_a_key_we_never_issued_is_refused_before_it_touches_the_disk():
    """The key comes back from a client, so it is matched rather than trusted."""
    assert recording_store.path_for("../../etc/passwd") is None
    assert recording_store.path_for("ab/cd/" + "z" * 64 + ".wav") is None
    assert recording_store.path_for("ab/cd/" + "a" * 64 + ".wav") is not None


async def test_a_row_whose_file_has_gone_is_a_404_and_stays_listed(
    client, session, price_book, speech_box
):
    """A restore that missed RECORDINGS_DIR, told apart from a deleted row."""
    token = await funded(client, session, "restored@example.com")
    await synthesize(client, token)
    row = (await client.get("/tts/recordings", headers=auth(token))).json()["recordings"][0]

    for path in recording_store.root().rglob("*.wav"):
        path.unlink()

    played = await client.get(f"/tts/recordings/{row['id']}/audio", headers=auth(token))
    assert played.status_code == 404
    assert played.json()["code"] == "recording_audio_missing"

    # Still listed: the row is the record that the synthesis happened, and it
    # is not the audio's job to prove it.
    assert len((await client.get("/tts/recordings", headers=auth(token))).json()["recordings"]) == 1


# --- listing ----------------------------------------------------------------


async def test_the_list_is_newest_first_and_pages_with_a_cursor(
    client, session, price_book, speech_box
):
    token = await funded(client, session, "pager@example.com")
    for index in range(3):
        speech_box.chunks = (f"clip-{index}".encode() * 4,)
        assert (await synthesize(client, token, text=f"{index}" * 100)).status_code == 200

    first = await client.get("/tts/recordings?limit=2", headers=auth(token))
    page = first.json()
    assert len(page["recordings"]) == 2
    assert page["page"]["has_more"] is True

    second = await client.get(
        f"/tts/recordings?limit=2&cursor={page['page']['next_cursor']}", headers=auth(token)
    )
    rest = second.json()
    assert len(rest["recordings"]) == 1
    assert rest["page"]["has_more"] is False

    seen = [row["id"] for row in page["recordings"] + rest["recordings"]]
    assert len(set(seen)) == 3, "a cursor that repeats or skips a row"
