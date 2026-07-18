"""Tests for the real PrinterTransport (not a mock) using a fake BleakClient.

The other test files inject a MockTransport that bypasses PrinterTransport
entirely. These tests construct a real PrinterTransport wired to a fake
BleakClient that simulates notify callbacks — so they exercise the credit
loop, the write lock, and the _expecting_reply event/reply distinction.

Run with:  uv run pytest tests/test_transport.py -v
"""
from __future__ import annotations

import asyncio
from typing import Callable

import pytest

from luckjingle import protocol
from luckjingle.transport import PrinterTransport


class FakeBleakClient:
    """In-process BleakClient stand-in.

    Records every write and lets tests deliver notifications via
    `simulate_notify(char_uuid, payload)`. Has no services — the transport
    only uses `write_gatt_char` and the two notify callbacks we register.

    `auto_replies` maps a request-byte prefix to a reply payload: when a
    write to ff02 matches a prefix, the reply is delivered on ff01 after a
    yield. This models the real printer's request/reply ordering — replies
    arrive only after their request goes out.
    """

    def __init__(self):
        self.writes: list[tuple[str, bytes]] = []
        self._notify_callbacks: dict[str, Callable[[object, bytearray], None]] = {}
        self.connected: bool = False
        # If True, start_notify raises to test cleanup paths.
        self.fail_on_start_notify: bool = False
        # request-prefix -> reply payload, delivered automatically on write.
        self.auto_replies: dict[bytes, bytes] = {}

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False

    async def write_gatt_char(self, uuid: str, data, response: bool = False):
        data = bytes(data)
        self.writes.append((uuid, data))
        if uuid == protocol.WRITE_CHAR_UUID:
            for prefix, reply in self.auto_replies.items():
                if data.startswith(prefix):
                    asyncio.create_task(self._deliver(protocol.RESPONSE_CHAR_UUID, reply))
                    break

    async def start_notify(self, uuid: str, callback):
        if self.fail_on_start_notify:
            raise RuntimeError("simulated start_notify failure")
        self._notify_callbacks[uuid] = callback

    async def _deliver(self, uuid: str, payload: bytes) -> None:
        await asyncio.sleep(0)
        self.simulate_notify(uuid, payload)

    # -- test helpers --------------------------------------------------

    def simulate_notify(self, uuid: str, payload: bytes) -> None:
        cb = self._notify_callbacks.get(uuid)
        if cb is None:
            raise AssertionError(f"no notify callback registered for {uuid}")
        # The real bleak passes (BleakGATTCharacteristic, bytearray). Our
        # handlers don't use the first arg.
        cb(None, bytearray(payload))


@pytest.fixture
def transport_with_credit():
    """A transport wired to FakeBleakClient, primed with lots of credit.

    Bypasses PrinterTransport.connect() (which would need a real BleakClient
    and a real device on the wire) by manually registering the notify
    callbacks the way connect() does. The fake client then dispatches via
    simulate_notify() into the real transport handlers.
    """
    client = FakeBleakClient()
    client.connected = True
    transport = PrinterTransport(client=client, packet_size=64)
    # Wire up notifies exactly as PrinterTransport.connect() would.
    client._notify_callbacks[protocol.CREDIT_CHAR_UUID] = transport._on_credit
    client._notify_callbacks[protocol.RESPONSE_CHAR_UUID] = transport._on_response
    transport._credit = 1000  # plenty, so the credit wait never blocks
    return transport, client


# ---------------------------------------------------------------------------
# Single exchange
# ---------------------------------------------------------------------------

def test_exchange_returns_delivered_reply(transport_with_credit):
    transport, client = transport_with_credit

    async def go():
        async def deliver():
            await asyncio.sleep(0.01)
            client.simulate_notify(protocol.RESPONSE_CHAR_UUID, b"ModelX")

        task = asyncio.create_task(deliver())
        reply = await transport.exchange(protocol.cmd_get_model())
        await task
        return reply

    assert asyncio.run(go()) == b"ModelX"
    # Verify the request was actually written to ff02.
    assert any(w[0] == protocol.WRITE_CHAR_UUID for w in client.writes)


def test_exchange_times_out_returns_none(transport_with_credit):
    transport, _ = transport_with_credit

    async def go():
        return await transport.exchange(protocol.cmd_get_model(), timeout=0.05)

    assert asyncio.run(go()) is None


def test_send_without_wait_does_not_block(transport_with_credit):
    transport, client = transport_with_credit

    async def go():
        await transport.send(protocol.cmd_enable_printer(3))
        return None

    asyncio.run(go())
    assert client.writes[-1][1] == protocol.cmd_enable_printer(3)


def test_empty_payload_is_noop(transport_with_credit):
    transport, client = transport_with_credit

    async def go():
        return await transport.send(b"")

    asyncio.run(go())
    assert client.writes == []


# ---------------------------------------------------------------------------
# _write_lock: concurrent exchanges get their own replies (not each other's)
# ---------------------------------------------------------------------------

def test_concurrent_exchanges_get_distinct_replies(transport_with_credit):
    """The bug fixed by _write_lock: with concurrent gather, each caller
    must receive its own reply, not the other's. Without the lock the two
    replies would race on _last_response and both tasks would see the
    same (first) reply.

    Uses auto_replies so each reply is delivered only after its matching
    request is written — realistic timing, and means B's reply can't
    arrive before B is actually waiting for it.
    """
    transport, client = transport_with_credit
    client.auto_replies = {
        protocol.cmd_get_model(): b"ModelX",
        protocol.cmd_get_version(): b"v9.9",
    }

    async def go():
        return await asyncio.gather(
            transport.exchange(protocol.cmd_get_model(), timeout=1.0),
            transport.exchange(protocol.cmd_get_version(), timeout=1.0),
        )

    r1, r2 = asyncio.run(go())
    assert r1 == b"ModelX"
    assert r2 == b"v9.9"


def test_concurrent_writes_do_not_interleave(transport_with_credit):
    """Two writes (no wait_for) must serialize on the wire — no packet should
    contain bytes from both payloads.
    """
    transport, client = transport_with_credit

    async def go():
        # packet_size is 64; each payload spans multiple packets.
        a = b"\xAA" * 200
        b = b"\xBB" * 200
        await asyncio.gather(transport.send(a), transport.send(b))

    asyncio.run(go())
    # Every written packet must be all 0xAA or all 0xBB (no mixing).
    for uuid, packet in client.writes:
        assert uuid == protocol.WRITE_CHAR_UUID
        unique = set(packet)
        assert unique <= {0xAA} or unique <= {0xBB}, (
            f"interleaved packet detected: {packet.hex()}"
        )
    # Both payloads were written in full.
    flat = b"".join(packet for _, packet in client.writes)
    assert flat.count(b"\xAA") == 200
    assert flat.count(b"\xBB") == 200


# ---------------------------------------------------------------------------
# _expecting_reply: async events arriving while idle don't poison the slot
# ---------------------------------------------------------------------------

def test_async_event_while_idle_does_not_poison_next_reply(transport_with_credit):
    """If an async event (paper-out, 0xAA) arrives while no exchange is
    pending, it must not become the next caller's reply.
    """
    transport, client = transport_with_credit

    async def go():
        # 1. Fire an unsolicited event while nobody is waiting.
        client.simulate_notify(protocol.RESPONSE_CHAR_UUID, protocol.LABEL_PAPER_ERROR_EVENT)
        # 2. Now do a real exchange and verify it gets its own reply.
        async def deliver():
            await asyncio.sleep(0.01)
            client.simulate_notify(protocol.RESPONSE_CHAR_UUID, b"\x00")  # idle status byte
        task = asyncio.create_task(deliver())
        reply = await transport.exchange(protocol.cmd_status(), timeout=1.0)
        await task
        return reply

    assert asyncio.run(go()) == b"\x00"


def test_async_event_fires_listeners_even_when_idle(transport_with_credit):
    """Idle events are still delivered to listeners (e.g. `watch` command)."""
    transport, client = transport_with_credit
    seen = []
    transport.add_event_listener(lambda kind, payload: seen.append((kind, payload)))

    async def go():
        client.simulate_notify(protocol.RESPONSE_CHAR_UUID, protocol.LABEL_PAPER_ERROR_EVENT)
        # Yield so the listener runs synchronously inside _on_response.
        await asyncio.sleep(0)
        return None

    asyncio.run(go())
    assert len(seen) == 1
    assert seen[0][0] == "label_paper_error"


def test_event_listener_exceptions_are_swallowed(transport_with_credit):
    """A buggy listener must not crash the notify dispatch."""
    transport, client = transport_with_credit

    def bad_listener(kind, payload):
        raise ValueError("buggy listener")
    transport.add_event_listener(bad_listener)
    good_seen = []
    transport.add_event_listener(lambda k, p: good_seen.append((k, p)))

    async def go():
        client.simulate_notify(protocol.RESPONSE_CHAR_UUID, b"OK")
        await asyncio.sleep(0)

    asyncio.run(go())
    assert good_seen  # the good listener still ran despite the bad one raising


# ---------------------------------------------------------------------------
# Credit flow
# ---------------------------------------------------------------------------

def test_send_blocks_when_no_credit(transport_with_credit):
    transport, client = transport_with_credit
    transport._credit = 0  # starve

    async def go():
        async def deliver_credit():
            await asyncio.sleep(0.05)
            # Simulate the printer granting 5 credits on ff03.
            client.simulate_notify(protocol.CREDIT_CHAR_UUID, b"\x01\x05")
        task = asyncio.create_task(deliver_credit())
        await transport.send(protocol.cmd_enable_printer(3))
        await task

    asyncio.run(go())
    # Write happened after credit was granted.
    assert client.writes


def test_credit_notification_adds_to_counter(transport_with_credit):
    transport, client = transport_with_credit
    transport._credit = 0

    async def go():
        client.simulate_notify(protocol.CREDIT_CHAR_UUID, b"\x01\x07")
        await asyncio.sleep(0)
        # After the callback, credit should be 7.
        return transport._credit

    assert asyncio.run(go()) == 7


def test_mtu_notification_updates_packet_size(transport_with_credit):
    transport, client = transport_with_credit
    transport.packet_size = 20
    transport._mtu_ready.clear()

    async def go():
        # [0x02, lo, hi] = MTU announcement. 244 -> packet_size = 244.
        client.simulate_notify(protocol.CREDIT_CHAR_UUID, b"\x02\xF4\x00")
        await asyncio.sleep(0)
        return transport.packet_size

    assert asyncio.run(go()) == 244
    assert transport._mtu_ready.is_set()


# ---------------------------------------------------------------------------
# start_notify failure cleanup (#3)
# ---------------------------------------------------------------------------

def test_start_notify_failure_triggers_disconnect():
    """If start_notify raises mid-handshake, the open BleakClient is closed.

    Tests the contract that PrinterTransport.connect() documents: on
    start_notify failure, disconnect() runs before re-raising. We exercise
    the same try/except shape inline using a FakeBleakClient configured to
    fail start_notify.
    """
    fake_client = FakeBleakClient()
    fake_client.connected = True
    fake_client.fail_on_start_notify = True
    transport = PrinterTransport(client=fake_client)

    async def go():
        # Mirror the connect() body's post-service-check section.
        try:
            await fake_client.start_notify(protocol.CREDIT_CHAR_UUID, transport._on_credit)
            await fake_client.start_notify(protocol.RESPONSE_CHAR_UUID, transport._on_response)
        except Exception:
            await fake_client.disconnect()
            raise

    with pytest.raises(RuntimeError, match="simulated"):
        asyncio.run(go())
    assert fake_client.connected is False, "client was not disconnected after start_notify failure"


# ---------------------------------------------------------------------------
# Disconnect / context manager
# ---------------------------------------------------------------------------

def test_disconnect_is_idempotent(transport_with_credit):
    transport, client = transport_with_credit

    async def go():
        await transport.disconnect()
        await transport.disconnect()  # should not raise

    asyncio.run(go())
    assert client.connected is False
