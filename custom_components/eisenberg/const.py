"""Constants for the Eisenberg integration."""

DOMAIN = "eisenberg"

CONF_DEVICE_ID = "device_id"
CONF_TRUST_COOKIE = "trust_cookie"
CONF_MEDIA_DIR = "media_dir"
CONF_DETECTION_TIMEOUT = "detection_timeout"
CONF_MEDIA_RETENTION_DAYS = "media_retention_days"
CONF_FFMPEG_STREAM = "ffmpeg_stream"

DEFAULT_DETECTION_TIMEOUT = 30
DEFAULT_MEDIA_RETENTION_DAYS = 14
# Default off: go2rtc reads Arlo's rtsps natively (in-process, HEVC
# passthrough, smooth). Opt in only where go2rtc's native RTSP client
# can't read the stream (black live view, issue #23) — then we route the
# source through ffmpeg, which reads Arlo's TLS stream correctly at the
# cost of an extra remux hop. See EisenbergCamera.stream_source.
DEFAULT_FFMPEG_STREAM = False

EVENT_MEDIA = "eisenberg_media"
