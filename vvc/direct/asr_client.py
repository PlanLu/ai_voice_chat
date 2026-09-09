import gzip
import json
import struct
import uuid

import websockets


class ASRClient:
    def __init__(self, api_key, resource_id, hotword_table_id=None):
        """保存豆包鉴权配置并初始化 WebSocket 状态。"""
        self.api_key = api_key
        self.resource_id = resource_id
        self.hotword_table_id = hotword_table_id

        self.url = (
            "wss://openspeech.bytedance.com"
            "/api/v3/sauc/bigmodel_async"
        )

        self.ws = None

    async def connect(self):
        """建立 ASR 长连接并发送识别配置。"""
        headers = {
            "X-Api-Key": self.api_key,
            "X-Api-Resource-Id": self.resource_id,
            "X-Api-Request-Id": str(uuid.uuid4()),
            "X-Api-Sequence": "-1"
        }

        self.ws = await websockets.connect(
            self.url,
            additional_headers=headers,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=5,
        )
        await self.ws.send(self.build_full_client_request())

    async def send_audio(self, chunk):
        """把一块麦克风 PCM 数据封装后发送给 ASR。"""
        await self.ws.send(self.build_audio_packet(chunk))

    async def receive(self):
        """接收并解析一条豆包 ASR 响应。"""
        data = await self.ws.recv()

        if not isinstance(data, bytes):
            return None

        header_size = data[0] & 0x0F
        message_type = data[1] >> 4
        flags = data[1] & 0x0F
        serialization = data[2] >> 4
        compression = data[2] & 0x0F

        payload = data[header_size * 4:]

        if flags & 0x01:
            payload = payload[4:]

        # 正常 ASR 响应
        if message_type == 0x09:

            payload_size = struct.unpack(">I", payload[:4])[0]
            payload = payload[4:4 + payload_size]

            if compression == 0x01:
                payload = gzip.decompress(payload)

            if serialization == 0x01:
                result = json.loads(payload.decode("utf-8"))
            else:
                result = payload

            return result

        # 错误响应
        if message_type == 0x0F:

            code = struct.unpack(">i", payload[:4])[0]
            size = struct.unpack(">I", payload[4:8])[0]

            error = payload[8:8 + size]

            if compression == 0x01:
                error = gzip.decompress(error)

            error = error.decode("utf-8")

            raise RuntimeError(
                f"豆包ASR错误: {code} - {error}"
            )

        return None

    async def close(self):
        """关闭当前 ASR WebSocket 连接。"""
        if self.ws:
            await self.ws.close()
            self.ws = None

    def build_audio_packet(self, chunk: bytes):
        """按豆包协议把 PCM 数据构造成音频请求包。"""
        payload = gzip.compress(chunk)
        header = bytes([
            0x11,
            0x20,  # Audio Only Request
            0x01,  # raw bytes + gzip
            0x00
        ])

        payload_size = struct.pack(">I", len(payload))

        return header + payload_size + payload

    def build_full_client_request(self):
        """构造包含说话人分离和 800 ms 判停的初始化请求。"""
        payload = {
            "user": {
                "uid": "test_user"
            },
            "audio": {
                "format": "pcm",
                "codec": "raw",
                "rate": 16000,
                "bits": 16,
                "channel": 1
            },
            "request": {
                "model_name": "bigmodel",
                "enable_itn": True,
                "enable_punc": True,
                "enable_ddc": False,
                "enable_speaker_info": True,
                "ssd_version": "200",
                "enable_nonstream": True,
                "show_utterances": True,
                "result_type": "single",
                # 由豆包服务端 VAD 在连续静音约 800 ms 后确认本句。
                "end_window_size": 800,
                "force_to_speech_time": 0
            }
        }

        if self.hotword_table_id:
            payload["request"]["corpus"] = {
                "boosting_table_id": self.hotword_table_id
            }

        payload = json.dumps(payload).encode("utf-8")
        payload = gzip.compress(payload)

        header = bytes([
            0x11,  # version=1, header size=4
            0x10,  # Full Client Request, 无 sequence
            0x11,  # JSON + gzip
            0x00
        ])

        payload_size = struct.pack(">I", len(payload))

        return header + payload_size + payload
