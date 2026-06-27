"""Tests for raw MQTT 3.1.1 packet codec."""

from eisenberg.mqtt_codec import (
    MQTTPublish,
    build_connect,
    build_disconnect,
    build_pingreq,
    build_subscribe,
    parse_connack,
    parse_packet_type,
    parse_publish,
    parse_suback,
    split_packets,
)
from eisenberg.mqtt_codec import (
    _encode_remaining_length as encode_remaining_length,
)


class TestBuildConnect:
    def test_builds_valid_packet(self) -> None:
        pkt = build_connect(
            client_id="test-client",
            username="user123",
            password="token-abc",
            keepalive=60,
        )
        assert pkt[0] == 0x10
        assert b"MQTT" in pkt
        assert b"test-client" in pkt
        assert b"user123" in pkt
        assert b"token-abc" in pkt

    def test_keepalive_encoded(self) -> None:
        pkt = build_connect(
            client_id="c",
            username="u",
            password="p",
            keepalive=60,
        )
        assert b"\x00\x3c" in pkt


class TestBuildSubscribe:
    def test_builds_valid_packet(self) -> None:
        pkt = build_subscribe(
            packet_id=1,
            topics=["d/cloud123/out/#", "u/user123/in/#"],
        )
        assert pkt[0] == 0x82
        assert b"d/cloud123/out/#" in pkt
        assert b"u/user123/in/#" in pkt


class TestBuildPingreq:
    def test_fixed_packet(self) -> None:
        assert build_pingreq() == bytes([0xC0, 0x00])


class TestBuildDisconnect:
    def test_fixed_packet(self) -> None:
        assert build_disconnect() == bytes([0xE0, 0x00])


class TestParsePacketType:
    def test_connack(self) -> None:
        assert parse_packet_type(bytes([0x20, 0x02, 0x00, 0x00])) == 2

    def test_suback(self) -> None:
        assert parse_packet_type(bytes([0x90, 0x04, 0x00, 0x01, 0x00, 0x00])) == 9

    def test_publish(self) -> None:
        assert parse_packet_type(bytes([0x30, 0x05, 0x00, 0x01, 0x74, 0x7B, 0x7D])) == 3

    def test_pingresp(self) -> None:
        assert parse_packet_type(bytes([0xD0, 0x00])) == 13


class TestParseConnack:
    def test_success(self) -> None:
        rc = parse_connack(bytes([0x20, 0x02, 0x00, 0x00]))
        assert rc == 0

    def test_bad_credentials(self) -> None:
        rc = parse_connack(bytes([0x20, 0x02, 0x00, 0x04]))
        assert rc == 4


class TestParseSuback:
    def test_single_granted(self) -> None:
        # 0x90, remaining=3, packet_id=0x0001, one return code 0x00 (QoS 0 granted)
        codes = parse_suback(bytes([0x90, 0x03, 0x00, 0x01, 0x00]))
        assert codes == [0x00]

    def test_mixed_grant_and_failure(self) -> None:
        # Two topics: first granted QoS 0, second denied (0x80).
        codes = parse_suback(bytes([0x90, 0x04, 0x00, 0x01, 0x00, 0x80]))
        assert codes == [0x00, 0x80]

    def test_many_topics(self) -> None:
        payload = bytes([0x00, 0x01]) + bytes([0x00, 0x00, 0x80, 0x00])
        pkt = bytes([0x90, len(payload)]) + payload
        assert parse_suback(pkt) == [0x00, 0x00, 0x80, 0x00]


class TestParsePublish:
    def test_simple_json_payload(self) -> None:
        topic_bytes = b"t"
        payload_bytes = b'{"key":"val"}'
        topic_len = len(topic_bytes).to_bytes(2, "big")
        remaining = topic_len + topic_bytes + payload_bytes
        pkt = bytes([0x30, len(remaining)]) + remaining

        result = parse_publish(pkt)
        assert isinstance(result, MQTTPublish)
        assert result.topic == "t"
        assert result.payload == b'{"key":"val"}'

    def test_longer_topic(self) -> None:
        topic = "d/CLOUD123/out/cameras/CAM456/is"
        payload = b'{"motionDetected":true}'
        topic_bytes = topic.encode()
        topic_len = len(topic_bytes).to_bytes(2, "big")
        remaining = topic_len + topic_bytes + payload
        pkt = bytes([0x30, len(remaining)]) + remaining

        result = parse_publish(pkt)
        assert result.topic == topic
        assert result.payload == payload

    def test_qos1_skips_packet_id(self) -> None:
        topic = "t"
        payload = b'{"x":1}'
        topic_bytes = topic.encode()
        topic_len = len(topic_bytes).to_bytes(2, "big")
        packet_id = b"\x00\x01"
        remaining = topic_len + topic_bytes + packet_id + payload
        pkt = bytes([0x32, len(remaining)]) + remaining

        result = parse_publish(pkt)
        assert result.topic == "t"
        assert result.payload == payload


def _publish_packet(topic: str, payload: bytes) -> bytes:
    """A complete QoS-0 PUBLISH packet for the given topic/payload."""
    tb = topic.encode()
    remaining = len(tb).to_bytes(2, "big") + tb + payload
    return bytes([0x30]) + encode_remaining_length(len(remaining)) + remaining


class TestSplitPackets:
    """Issue #13: MQTT-over-WebSocket does not align control packets to WS
    frame boundaries. A frame may carry several packets, and one packet may
    span frames. split_packets() must extract every complete packet and hand
    back the trailing partial bytes to prepend to the next frame."""

    def test_empty_buffer(self) -> None:
        assert split_packets(b"") == ([], b"")

    def test_single_complete_packet(self) -> None:
        p = _publish_packet("a", b'{"n":1}')
        assert split_packets(p) == ([p], b"")

    def test_two_packets_in_one_buffer(self) -> None:
        p1 = _publish_packet("a", b'{"n":1}')
        p2 = _publish_packet("b", b'{"n":2}')
        packets, rem = split_packets(p1 + p2)
        assert packets == [p1, p2]
        assert rem == b""

    def test_incomplete_payload_held_as_remainder(self) -> None:
        p = _publish_packet("d/CLOUD/out/x", b'{"big":"payload"}')
        partial = p[:-3]
        packets, rem = split_packets(partial)
        assert packets == []
        assert rem == partial

    def test_partial_header_byte_held(self) -> None:
        packets, rem = split_packets(bytes([0x30]))
        assert packets == []
        assert rem == bytes([0x30])

    def test_complete_packet_plus_partial_next(self) -> None:
        p1 = _publish_packet("a", b"{}")
        p2 = _publish_packet("b", b'{"x":1}')
        packets, rem = split_packets(p1 + p2[:2])
        assert packets == [p1]
        assert rem == p2[:2]

    def test_multibyte_remaining_length(self) -> None:
        # A 200-byte payload forces a 2-byte remaining-length varint.
        p = _publish_packet("t", b"x" * 200)
        assert len(p) > 200
        packets, rem = split_packets(p + bytes([0x30]))
        assert packets == [p]
        assert rem == bytes([0x30])

    def test_incomplete_varint_held(self) -> None:
        # Header byte + a remaining-length byte with the continuation bit set
        # but no follow-on byte yet: cannot know the length, so hold it all.
        buf = bytes([0x30, 0x80])
        packets, rem = split_packets(buf)
        assert packets == []
        assert rem == buf
