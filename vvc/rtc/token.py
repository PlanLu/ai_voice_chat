import base64
import hashlib
import hmac
import secrets
import struct
import time


VERSION = "001"
PRIV_PUBLISH_STREAM = 0
PRIV_PUBLISH_AUDIO_STREAM = 1
PRIV_PUBLISH_VIDEO_STREAM = 2
PRIV_PUBLISH_DATA_STREAM = 3
PRIV_SUBSCRIBE_STREAM = 4


def _bytes(value):
    raw = value if isinstance(value, bytes) else value.encode("utf-8")
    return struct.pack("<H", len(raw)) + raw


def _privileges(values):
    packed = struct.pack("<H", len(values))
    for privilege, expires_at in values.items():
        packed += struct.pack("<HI", privilege, expires_at)
    return packed


def create_rtc_token(app_id, app_key, room_id, user_id, ttl_seconds=3600, *, now=None, nonce=None):
    """生成火山 RTC AccessToken 001；AppKey 只能保存在服务端。"""
    if not all((app_id, app_key, room_id, user_id)):
        raise ValueError("AppId、AppKey、RoomId 和 UserId 均不能为空")
    if len(app_id) != 24:
        raise ValueError("RTC_APP_ID 应为 24 个字符")
    if ttl_seconds <= 0:
        raise ValueError("ttl_seconds 必须大于 0")

    issued_at = int(time.time() if now is None else now)
    expires_at = issued_at + int(ttl_seconds)
    nonce = secrets.randbits(32) if nonce is None else nonce
    privileges = {
        PRIV_PUBLISH_STREAM: expires_at,
        PRIV_PUBLISH_AUDIO_STREAM: expires_at,
        PRIV_PUBLISH_VIDEO_STREAM: expires_at,
        PRIV_PUBLISH_DATA_STREAM: expires_at,
        PRIV_SUBSCRIBE_STREAM: expires_at,
    }
    message = (
        struct.pack("<III", nonce, issued_at, expires_at)
        + _bytes(room_id)
        + _bytes(user_id)
        + _privileges(privileges)
    )
    signature = hmac.new(app_key.encode("utf-8"), message, hashlib.sha256).digest()
    content = _bytes(message) + _bytes(signature)
    return VERSION + app_id + base64.b64encode(content).decode("ascii")
