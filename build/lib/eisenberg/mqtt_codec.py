"""Raw MQTT 3.1.1 packet construction and parsing.

Only implements the subset needed for Arlo:
CONNECT, CONNACK, SUBSCRIBE, SUBACK, PUBLISH, PINGREQ, PINGRESP, DISCONNECT.

No external MQTT library needed — the binary protocol is straightforward.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass


@dataclass(frozen=True)
class MQTTPublish:
    """Parsed MQTT PUBLISH packet."""

    topic: str
    payload: bytes


def _encode_remaining_length(length: int) -> bytes:
    """Encode MQTT remaining length (variable-length encoding)."""
    result = bytearray()
    while True:
        byte = length & 0x7F
        length >>= 7
        if length > 0:
            byte |= 0x80
        result.append(byte)
        if length == 0:
            break
    return bytes(result)


def _decode_remaining_length(data: bytes, start: int) -> tuple[int, int]:
    """Decode MQTT remaining length. Returns (length, next_index)."""
    idx = start
    remaining = 0
    multiplier = 1
    while idx < len(data):
        byte = data[idx]
        remaining += (byte & 0x7F) * multiplier
        multiplier *= 128
        idx += 1
        if (byte & 0x80) == 0:
            break
    return remaining, idx


def _encode_utf8_string(s: str) -> bytes:
    """Encode a UTF-8 string with 2-byte length prefix."""
    encoded = s.encode("utf-8")
    return struct.pack("!H", len(encoded)) + encoded


def build_connect(
    client_id: str,
    username: str,
    password: str,
    keepalive: int,
) -> bytes:
    """Build an MQTT CONNECT packet."""
    var_header = (
        b"\x00\x04MQTT"  # Protocol name
        b"\x04"  # Protocol level (MQTT 3.1.1)
        b"\xc2"  # Connect flags: username + password + clean session
        + struct.pack("!H", keepalive)
    )

    payload = (
        _encode_utf8_string(client_id)
        + _encode_utf8_string(username)
        + _encode_utf8_string(password)
    )

    remaining = var_header + payload
    return bytes([0x10]) + _encode_remaining_length(len(remaining)) + remaining


def build_subscribe(packet_id: int, topics: list[str]) -> bytes:
    """Build an MQTT SUBSCRIBE packet."""
    payload = struct.pack("!H", packet_id)
    for topic in topics:
        payload += _encode_utf8_string(topic) + b"\x00"  # QoS 0

    return bytes([0x82]) + _encode_remaining_length(len(payload)) + payload


def build_pingreq() -> bytes:
    """Build an MQTT PINGREQ packet."""
    return bytes([0xC0, 0x00])


def build_disconnect() -> bytes:
    """Build an MQTT DISCONNECT packet."""
    return bytes([0xE0, 0x00])


def take_packet(buffer: bytes) -> tuple[bytes | None, bytes]:
    """Consume one complete MQTT packet from the front of ``buffer``.

    Returns ``(packet, remainder)``. ``packet`` is ``None`` (and the buffer
    returned untouched) when the front does not yet hold a full fixed header
    (type byte + complete remaining-length varint) or a full payload — the
    caller should read more bytes and retry.

    MQTT-over-WebSocket does NOT align control packets to WebSocket frame
    boundaries: a frame may carry several packets, and one packet may span
    frames. This is the primitive that lets us decode exactly one packet and
    keep the rest verbatim, so bytes trailing a CONNACK/SUBACK (a coalesced
    PUBLISH) are preserved rather than discarded (issue #13).
    """
    n = len(buffer)
    if n < 1:
        return None, bytes(buffer)
    # Decode the remaining-length varint starting after the type byte. Bail
    # the moment we run out of bytes mid-varint — length is unknown yet.
    remaining = 0
    multiplier = 1
    pos = 1
    complete = False
    while pos < n:
        byte = buffer[pos]
        remaining += (byte & 0x7F) * multiplier
        pos += 1
        if (byte & 0x80) == 0:
            complete = True
            break
        multiplier *= 128
        if multiplier > 128**3:  # varint is max 4 bytes per spec
            complete = True
            break
    if not complete:
        return None, bytes(buffer)
    end = pos + remaining
    if end > n:
        return None, bytes(buffer)  # full payload not arrived yet
    return bytes(buffer[:end]), bytes(buffer[end:])


def split_packets(buffer: bytes) -> tuple[list[bytes], bytes]:
    """Split a byte buffer into every complete MQTT packet plus a remainder.

    Loops :func:`take_packet`. Returns ``(packets, remainder)`` where
    ``remainder`` is the trailing incomplete bytes to prepend to the next
    chunk. See :func:`take_packet` for why this is needed (issue #13).
    """
    packets: list[bytes] = []
    rest = bytes(buffer)
    while True:
        packet, rest = take_packet(rest)
        if packet is None:
            break
        packets.append(packet)
    return packets, rest


def parse_packet_type(data: bytes) -> int:
    """Extract packet type from first byte (upper 4 bits)."""
    return (data[0] >> 4) & 0x0F


def parse_connack(data: bytes) -> int:
    """Parse CONNACK packet, return the return code (0 = success)."""
    return data[3]


def parse_suback(data: bytes) -> list[int]:
    """Parse a SUBACK packet into its per-topic return codes.

    The payload is one byte per subscribed topic, in request order:
    0x00/0x01/0x02 = granted at that QoS, 0x80 = failure (the broker
    refused the subscription, e.g. an ACL denied that topic filter).
    """
    _, idx = _decode_remaining_length(data, 1)
    idx += 2  # skip the 2-byte packet identifier
    return list(data[idx:])


def parse_publish(data: bytes) -> MQTTPublish:
    """Parse an MQTT PUBLISH packet into topic + payload."""
    _, idx = _decode_remaining_length(data, 1)

    topic_len = struct.unpack("!H", data[idx : idx + 2])[0]
    idx += 2

    topic = data[idx : idx + topic_len].decode("utf-8")
    idx += topic_len

    qos = (data[0] >> 1) & 0x03
    if qos > 0:
        idx += 2  # Skip packet ID

    payload = data[idx:]
    return MQTTPublish(topic=topic, payload=payload)
