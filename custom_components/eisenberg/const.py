"""Constants for the Eisenberg integration."""

DOMAIN = "eisenberg"

CONF_DEVICE_ID = "device_id"
CONF_TRUST_COOKIE = "trust_cookie"
CONF_MEDIA_DIR = "media_dir"
CONF_DETECTION_TIMEOUT = "detection_timeout"
CONF_MEDIA_RETENTION_DAYS = "media_retention_days"
CONF_FFMPEG_STREAM = "ffmpeg_stream"

# The only cookie worth persisting. Arlo's responses also carry transient
# session cookies (__cf_bm, AWSALB, AWSALBCORS, JSESSIONID) which expire fast
# and are worse than useless in the config entry: storing them as "the trust
# cookie" is what overwrote the real one (#37). Every reader and writer of
# CONF_TRUST_COOKIE must agree on this prefix, so it lives here rather than
# being spelled out at each site.
TRUST_COOKIE_PREFIX = "browser_trust_"

DEFAULT_DETECTION_TIMEOUT = 30
# Arlo reports a doorbell press but never its release; the "Doorbell pressed"
# binary sensor turns itself off again after this many seconds.
DOORBELL_PRESS_RESET_SECONDS = 10
# Event type fired by the doorbell event entity on every press.
DOORBELL_EVENT_RING = "ring"
DEFAULT_MEDIA_RETENTION_DAYS = 14
# Default off: go2rtc reads Arlo's rtsps natively (in-process, HEVC
# passthrough, smooth). Opt in only where go2rtc's native RTSP client
# can't read the stream (black live view, issue #23) — then we route the
# source through ffmpeg, which reads Arlo's TLS stream correctly at the
# cost of an extra remux hop. See EisenbergCamera.stream_source.
DEFAULT_FFMPEG_STREAM = False

EVENT_MEDIA = "eisenberg_media"
