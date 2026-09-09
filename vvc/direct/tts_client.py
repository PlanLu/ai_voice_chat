import asyncio
import json
import struct
import uuid
from contextlib import suppress

import websockets
from websockets.exceptions import ConnectionClosed
from websockets.protocol import State


class TTSClient:
    """负责豆包双向流式 TTS 协议，将文本流转换为 PCM 音频流。"""

    URL = "wss://openspeech.bytedance.com/api/v3/tts/bidirection"

    EVENT_START_CONNECTION = 1
    EVENT_FINISH_CONNECTION = 2
    EVENT_CONNECTION_STARTED = 50
    EVENT_CONNECTION_FAILED = 51
    EVENT_START_SESSION = 100
    EVENT_FINISH_SESSION = 102
    EVENT_SESSION_STARTED = 150
    EVENT_SESSION_FINISHED = 152
    EVENT_SESSION_FAILED = 153
    EVENT_TASK_REQUEST = 200
    EVENT_TTS_RESPONSE = 352

    def __init__(self, api_key, resource_id, voice_type, sample_rate=24000):
        """保存鉴权、音色及 PCM 采样率配置。"""
        self.api_key = api_key
        self.resource_id = resource_id
        self.voice_type = voice_type
        self.sample_rate = sample_rate
        self._ws = None
        self._lock = asyncio.Lock()

    async def connect(self):
        """预先建立并初始化可供多个 TTS Session 复用的长连接。"""
        async with self._lock:
            await self._ensure_connected()

    async def close(self):
        """发送连接结束事件，并关闭持久 WebSocket。"""
        async with self._lock:
            if self._is_connected():
                with suppress(ConnectionClosed):
                    await self._ws.send(
                        self._build_packet(self.EVENT_FINISH_CONNECTION, {})
                    )
            await self._drop_connection()

    async def stream(self, text_stream):
        """在持久连接中创建独立 Session，将文本流转换为 PCM 音频流。"""
        async with self._lock:
            await self._ensure_connected()
            ws = self._ws
            session_id = str(uuid.uuid4())
            session_ready = asyncio.Event()
            sender = asyncio.create_task(
                self._send_text_stream(
                    ws, session_id, text_stream, session_ready
                )
            )

            try:
                await ws.send(
                    self._build_packet(
                        self.EVENT_START_SESSION,
                        self._session_config(),
                        session_id,
                    )
                )
                await self._expect_event(ws, self.EVENT_SESSION_STARTED)
                session_ready.set()

                async for audio in self._receive_audio(ws, sender):
                    yield audio

                await sender
            except BaseException:
                # 会话异常后连接状态无法保证干净，下一轮重新建立连接。
                await self._drop_connection()
                raise
            finally:
                if not sender.done():
                    sender.cancel()
                    await asyncio.gather(sender, return_exceptions=True)

    async def _ensure_connected(self):
        """复用健康连接；连接不存在或已断开时重新建立并握手。"""
        if self._is_connected():
            return

        await self._drop_connection()
        headers = {
            "X-Api-Key": self.api_key,
            "X-Api-Resource-Id": self.resource_id,
            "X-Api-Connect-Id": str(uuid.uuid4()),
        }

        try:
            self._ws = await websockets.connect(
                self.URL,
                additional_headers=headers,
                ping_interval=20,
                ping_timeout=20,
                close_timeout=5,
            )
            await self._ws.send(
                self._build_packet(self.EVENT_START_CONNECTION, {})
            )
            await self._expect_event(
                self._ws, self.EVENT_CONNECTION_STARTED
            )
        except Exception:
            await self._drop_connection()
            raise

    def _is_connected(self):
        """判断当前持久 WebSocket 是否仍处于可发送状态。"""
        return self._ws is not None and self._ws.state is State.OPEN

    async def _drop_connection(self):
        """无条件关闭并清空当前连接，用于退出或异常恢复。"""
        ws, self._ws = self._ws, None
        if ws is not None:
            with suppress(Exception):
                await ws.close()

    def _session_config(self):
        """构造 StartSession 所需的音色和 PCM 输出参数。"""
        return {
            "event": self.EVENT_START_SESSION,
            "namespace": "BidirectionalTTS",
            "user": {"uid": "voice_chat_user"},
            "req_params": {
                "speaker": self.voice_type,
                "audio_params": {
                    "format": "pcm",
                    "sample_rate": self.sample_rate,
                },
            },
        }

    async def _send_text_stream(
        self, ws, session_id, text_stream, session_ready
    ):
        """提前读取 LLM 文本，并在 TTS Session 就绪后依次发送。"""
        async for text in text_stream:
            if not text:
                continue
            await session_ready.wait()
            payload = {
                "event": self.EVENT_TASK_REQUEST,
                "namespace": "BidirectionalTTS",
                "req_params": {"text": text},
            }
            await ws.send(
                self._build_packet(self.EVENT_TASK_REQUEST, payload, session_id)
            )

        await session_ready.wait()
        await ws.send(self._build_packet(self.EVENT_FINISH_SESSION, {}, session_id))

    async def _receive_audio(self, ws, sender):
        """并发监听 TTS 音频和文本发送异常，直至会话自然结束。"""
        receive_task = asyncio.create_task(ws.recv())
        sender_task = sender

        try:
            while True:
                tasks = {receive_task}
                if sender_task:
                    tasks.add(sender_task)

                done, _ = await asyncio.wait(
                    tasks, return_when=asyncio.FIRST_COMPLETED
                )

                if sender_task in done:
                    error = sender_task.exception()
                    if error:
                        raise error
                    sender_task = None

                if receive_task not in done:
                    continue

                data = receive_task.result()
                message = self._parse_response(data)
                self._raise_for_error(message)

                if message["event"] == self.EVENT_TTS_RESPONSE and message["audio"]:
                    yield message["audio"]
                if message["event"] == self.EVENT_SESSION_FINISHED:
                    break

                receive_task = asyncio.create_task(ws.recv())
        finally:
            if not receive_task.done():
                receive_task.cancel()
                await asyncio.gather(receive_task, return_exceptions=True)

    async def _expect_event(self, ws, expected_event):
        """等待指定的握手事件，遇到错误事件时立即抛出异常。"""
        while True:
            message = self._parse_response(await ws.recv())
            self._raise_for_error(message)
            if message["event"] == expected_event:
                return

    def _raise_for_error(self, message):
        """把服务端错误帧和失败事件转换为易读的 Python 异常。"""
        if message["error"]:
            raise RuntimeError(message["error"])
        if message["event"] in {
            self.EVENT_CONNECTION_FAILED,
            self.EVENT_SESSION_FAILED,
        }:
            raise RuntimeError(f"豆包TTS会话失败: {message['payload']}")

    @staticmethod
    def _build_packet(event, payload, session_id=""):
        """按豆包 V3 大端二进制协议构造客户端事件包。"""
        body = json.dumps(payload).encode("utf-8")
        packet = bytes([0x11, 0x14, 0x10, 0x00]) + struct.pack(">i", event)

        if session_id:
            session = session_id.encode("utf-8")
            packet += struct.pack(">I", len(session)) + session

        return packet + struct.pack(">I", len(body)) + body

    def _parse_response(self, data):
        """解析服务端事件包，统一返回事件、音频、负载和错误信息。"""
        result = {"event": 0, "audio": b"", "payload": None, "error": ""}
        if not isinstance(data, bytes) or len(data) < 8:
            return result

        message_type = data[1]
        offset = (data[0] & 0x0F) * 4

        if message_type >> 4 == 0x0F:
            code, offset = self._read_int(data, offset)
            payload, _ = self._read_bytes(data, offset)
            detail = payload.decode("utf-8", errors="replace")
            result["error"] = f"豆包TTS错误: {code} - {detail}"
            return result

        event, offset = self._read_int(data, offset)
        result["event"] = event

        # 连接级事件携带 connection_id，其余事件携带 session_id，结构相同。
        _, offset = self._read_bytes(data, offset)
        payload, _ = self._read_bytes(data, offset)

        if message_type >> 4 == 0x0B:
            result["audio"] = payload
        elif payload:
            try:
                result["payload"] = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                result["payload"] = payload.decode("utf-8", errors="replace")

        return result

    @staticmethod
    def _read_int(data, offset):
        """从指定位置读取一个大端有符号 32 位整数。"""
        if offset + 4 > len(data):
            return 0, len(data)
        return struct.unpack_from(">i", data, offset)[0], offset + 4

    @staticmethod
    def _read_bytes(data, offset):
        """读取一个由 4 字节长度前缀描述的二进制字段。"""
        if offset + 4 > len(data):
            return b"", len(data)
        size = struct.unpack_from(">I", data, offset)[0]
        offset += 4
        end = min(offset + size, len(data))
        return data[offset:end], end
