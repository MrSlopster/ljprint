"""LuckJingle printer — BLE transport with credit-based flow control.

Single class: PrinterTransport. Handles GATT connect, MTU/credit negotiation
on `ff03`, response handling on `ff01`, and chunked writes to `ff02`.

Protocol details: PROTOCOL.md §1.
"""
from __future__ import annotations

import asyncio
import logging
import warnings
from dataclasses import dataclass, field
from typing import Optional

from bleak import BleakClient
from bleak.backends.characteristic import BleakGATTCharacteristic

from . import protocol

LOGGER = logging.getLogger("luckjingle.transport")

# Time the printer has to deliver a credit notification before we give up.
CREDIT_TIMEOUT_S = 30.0
# Service UUID prefix used to locate the printer service; the minor suffix of
# the characteristic UUIDs may differ across clones (PROTOCOL.md §1.1).
SERVICE_UUID_PREFIX = "0000ff00"
# Time to wait for a printer reply on ff01 when one is expected.
RESPONSE_TIMEOUT_S = 15.0
# How long to wait for the printer's initial MTU announcement.
MTU_ANNOUNCE_TIMEOUT_S = 10.0


def resolve_service_chars(services) -> Optional[tuple[str, str, str]]:
    """Locate (write, response, credit) characteristic UUIDs per PROTOCOL.md §1.1.

    The service is matched by the `0000ff00` prefix. Exact canonical
    characteristic UUIDs are preferred; clones with shifted minor suffixes
    fall back to properties — the write role goes to the characteristic with
    write-without-response, and the two notify characteristics map to
    response/credit in ascending UUID order (ff01 < ff03).
    """
    service = next(
        (s for s in services if s.uuid.lower().startswith(SERVICE_UUID_PREFIX)), None
    )
    if service is None:
        return None
    chars = list(service.characteristics)

    def by_uuid(uuid: str):
        return next((c for c in chars if c.uuid.lower() == uuid), None)

    write = by_uuid(protocol.WRITE_CHAR_UUID)
    if write is None:
        write = next((c for c in chars if "write-without-response" in c.properties), None)
    if write is None:
        write = next((c for c in chars if "write" in c.properties), None)
    if write is None:
        return None
    notifies = sorted(
        (c for c in chars if c is not write and "notify" in c.properties),
        key=lambda c: c.uuid.lower(),
    )
    response = by_uuid(protocol.RESPONSE_CHAR_UUID)
    credit = by_uuid(protocol.CREDIT_CHAR_UUID)
    if response is None:
        response = next((c for c in notifies if c is not credit), None)
    if credit is None:
        credit = next((c for c in notifies if c is not response), None)
    if response is None or credit is None:
        return None
    return write.uuid, response.uuid, credit.uuid


@dataclass
class PrinterTransport:
    """Credit-aware BLE client for the LuckJingle printer service.

    Single-writer contract: all send/exchange calls share `_write_lock`, so
    concurrent tasks serialize. This prevents interleaved writes on ff02 and
    ensures each requester's wait_for reply is matched to its own request.
    """

    client: BleakClient
    packet_size: int = protocol.DEFAULT_PACKET_SIZE
    write_uuid: str = protocol.WRITE_CHAR_UUID
    response_uuid: str = protocol.RESPONSE_CHAR_UUID
    credit_uuid: str = protocol.CREDIT_CHAR_UUID
    _credit: int = 0
    _credit_event: asyncio.Event = field(default_factory=asyncio.Event)
    _response_event: asyncio.Event = field(default_factory=asyncio.Event)
    _last_response: bytes = b""
    _mtu_ready: asyncio.Event = field(default_factory=asyncio.Event)
    _event_listeners: list = field(default_factory=list)
    # Serialises ALL writes. Concurrent writers (e.g. an in-flight print job
    # plus a status query, or two concurrent settings calls) would otherwise
    # interleave byte streams on ff02 and corrupt both.
    _write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Set while a sender is actively waiting for a reply. When False, ff01
    # notifications are async events (paper-out, 0xAA, label-error) and must
    # NOT be stored as the next caller's reply — they go to listeners only.
    _expecting_reply: bool = False

    # -----------------------------------------------------------------
    # Connection
    # -----------------------------------------------------------------

    @classmethod
    async def connect(cls, mac: str, *, scan_timeout: float = 20.0) -> "PrinterTransport":
        client = BleakClient(mac, timeout=scan_timeout)
        await client.connect()
        resolved = resolve_service_chars(client.services)
        if resolved is None:
            await client.disconnect()
            raise RuntimeError(
                f"Device {mac} does not expose the LuckJingle printer service "
                f"({SERVICE_UUID_PREFIX}* with write + two notify characteristics). "
                "Not a LuckJingle printer?"
            )
        write_uuid, response_uuid, credit_uuid = resolved
        transport = cls(client=client, write_uuid=write_uuid,
                        response_uuid=response_uuid, credit_uuid=credit_uuid)
        try:
            await client.start_notify(credit_uuid, transport._on_credit)
            await client.start_notify(response_uuid, transport._on_response)
        except Exception:
            # start_notify can fail mid-handshake (flaky adapter, race with
            # disconnect). Don't leak the open BleakClient.
            await client.disconnect()
            raise
        try:
            await asyncio.wait_for(transport._mtu_ready.wait(), timeout=MTU_ANNOUNCE_TIMEOUT_S)
        except asyncio.TimeoutError:
            LOGGER.warning("No MTU notification received; using default packet size.")
        return transport

    async def disconnect(self) -> None:
        try:
            await self.client.disconnect()
        except Exception as exc:
            LOGGER.debug("disconnect error (ignored): %s", exc)

    async def __aenter__(self) -> "PrinterTransport":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.disconnect()

    # -----------------------------------------------------------------
    # GATT notify callbacks
    # -----------------------------------------------------------------

    def _on_credit(self, _char: BleakGATTCharacteristic, data: bytearray) -> None:
        if len(data) == 3 and data[0] == 0x02:
            mtu = data[1] | (data[2] << 8)
            # PROTOCOL.md §1.2 says to request ATT_MTU = 3 + mtu, but bleak
            # cannot initiate an MTU exchange — the OS negotiates on its own.
            # Clamp to the link's actual ATT_MTU so a chunk never exceeds
            # what write-without-response can carry (ATT_MTU - 3). BlueZ
            # reports (with a UserWarning) the spec-minimum 23 when it never
            # acquired the MTU; treat that as unknown and trust the printer's
            # announcement, which is derived from the negotiated link MTU.
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    link_mtu = int(getattr(self.client, "mtu_size", 0))
            except Exception:
                link_mtu = 0
            usable = min(mtu, link_mtu - 3) if link_mtu > 23 else mtu
            if usable >= protocol.DEFAULT_PACKET_SIZE:
                self.packet_size = usable
            LOGGER.debug("printer announced MTU payload=%d, link ATT_MTU=%d -> packet_size=%d",
                         mtu, link_mtu, self.packet_size)
            self._mtu_ready.set()
            return
        if len(data) == 2 and data[0] == 0x01:
            n = data[1]
            self._credit += n
            LOGGER.debug("credit +%d -> %d", n, self._credit)
            if self._credit > 0:
                self._credit_event.set()

    def _on_response(self, _char: BleakGATTCharacteristic, data: bytearray) -> None:
        payload = bytes(data)
        kind = protocol.classify_response(payload)
        LOGGER.debug("response [%s]: %s", kind, payload.hex())
        # Always dispatch to listeners (e.g. the `watch` command) regardless
        # of whether anyone is waiting for a reply.
        for listener in list(self._event_listeners):
            try:
                listener(kind, payload)
            except Exception as exc:
                LOGGER.warning("event listener raised: %s", exc)
        # Consume the expectation atomically: only the FIRST notification
        # after send becomes the reply. This closes the race where a fast
        # second event arrives between event.set() and the waiter's `finally`
        # block (which would otherwise clobber _last_response before the
        # waiter reads it).
        if self._expecting_reply:
            self._last_response = payload
            self._response_event.set()
            self._expecting_reply = False

    def add_event_listener(self, listener) -> None:
        """Register `listener(kind: str, payload: bytes)` for every ff01 notification."""
        self._event_listeners.append(listener)

    def remove_event_listener(self, listener) -> None:
        try:
            self._event_listeners.remove(listener)
        except ValueError:
            pass

    # -----------------------------------------------------------------
    # Sending
    # -----------------------------------------------------------------

    async def send(self, payload: bytes, *, wait_for: bool = False,
                   timeout: float = RESPONSE_TIMEOUT_S) -> Optional[bytes]:
        """Send `payload` to write char ff02, respecting credits.

        All writes hold `_write_lock` for their full duration so concurrent
        callers can't interleave byte streams on the wire. When `wait_for`
        is set, the caller holds the lock across the wait too, so the reply
        it reads is guaranteed to be its own.
        """
        if not payload:
            return None
        async with self._write_lock:
            if not wait_for:
                await self._write_payload(payload)
                return None
            return await self._send_and_wait(payload, timeout)

    async def _send_and_wait(self, payload: bytes, timeout: float) -> Optional[bytes]:
        # Caller already holds _write_lock.
        self._response_event.clear()
        self._expecting_reply = True
        try:
            await self._write_payload(payload)
            try:
                await asyncio.wait_for(self._response_event.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                LOGGER.warning("timed out waiting for printer response")
                return None
            return self._last_response
        finally:
            self._expecting_reply = False

    async def _write_payload(self, payload: bytes) -> None:
        offset = 0
        total = len(payload)
        while offset < total:
            if self._credit <= 0:
                self._credit_event.clear()
                try:
                    await asyncio.wait_for(self._credit_event.wait(), timeout=CREDIT_TIMEOUT_S)
                except asyncio.TimeoutError:
                    raise RuntimeError(
                        f"printer stopped granting flow-control credits for "
                        f"{CREDIT_TIMEOUT_S:.0f}s (asleep, off, or out of range?)"
                    ) from None
            reserved = 1 if self._credit >= 3 else 0
            chunks_affordable = max(1, self._credit - reserved)
            chunks_needed = -(-(total - offset) // self.packet_size)
            chunks_this_round = min(chunks_affordable, chunks_needed)
            for _ in range(chunks_this_round):
                end = min(offset + self.packet_size, total)
                packet = payload[offset:end]
                await self.client.write_gatt_char(
                    self.write_uuid, packet, response=False
                )
                self._credit -= 1
                offset = end
                if offset >= total:
                    break

    async def exchange(self, payload: bytes, *, timeout: float = RESPONSE_TIMEOUT_S) -> Optional[bytes]:
        """Convenience: send and wait for a reply (serialized)."""
        return await self.send(payload, wait_for=True, timeout=timeout)
