import asyncio
import gzip
import json
import struct
import unittest
from unittest.mock import AsyncMock, patch

from websockets.protocol import State

from vvc.direct.audio_player import AudioPlayer
from vvc.direct.application import (
    collect_conversation,
    receive_final_texts,
    send_audio_loop,
)
from vvc.direct.asr_client import ASRClient
from vvc.direct.recorder import Recorder
from vvc.direct.tts_client import TTSClient


def accepting_results():
    """创建处于接收状态的识别结果开关。"""
    event = asyncio.Event()
    event.set()
    return event


def event_packet(message_type, event, session_id, payload):
    """构造测试用的豆包服务端事件包。"""
    session = session_id.encode("utf-8")
    return (
        bytes([0x11, message_type, 0x00, 0x00])
        + struct.pack(">iI", event, len(session))
        + session
        + struct.pack(">I", len(payload))
        + payload
    )


class TTSClientTests(unittest.TestCase):
    def setUp(self):
        """为每个测试创建独立的 TTS 客户端。"""
        self.client = TTSClient("key", "seed-tts-2.0", "voice")

    def test_session_uses_pcm_configuration(self):
        """验证双向会话请求使用预期的 PCM 参数。"""
        payload = self.client._session_config()

        self.assertEqual(payload["namespace"], "BidirectionalTTS")
        self.assertEqual(payload["req_params"]["speaker"], "voice")
        self.assertEqual(payload["req_params"]["audio_params"]["format"], "pcm")
        self.assertEqual(payload["req_params"]["audio_params"]["sample_rate"], 24000)

    def test_builds_event_packet_with_session_id(self):
        """验证客户端事件包包含事件号、会话 ID 和 JSON。"""
        packet = self.client._build_packet(200, {"value": 1}, "session")

        self.assertEqual(packet[:4], bytes([0x11, 0x14, 0x10, 0x00]))
        self.assertEqual(struct.unpack_from(">i", packet, 4)[0], 200)
        session_size = struct.unpack_from(">I", packet, 8)[0]
        self.assertEqual(packet[12:12 + session_size], b"session")

    def test_parses_audio_event(self):
        """验证音频事件能够提取 PCM 字节。"""
        packet = event_packet(0xB4, 352, "session", b"pcm-data")
        message = self.client._parse_response(packet)

        self.assertEqual(message["event"], 352)
        self.assertEqual(message["audio"], b"pcm-data")

    def test_parses_session_finished(self):
        """验证会话结束事件能够提取 JSON 负载。"""
        payload = json.dumps({"status": "ok"}).encode("utf-8")
        packet = event_packet(0x94, 152, "session", payload)
        message = self.client._parse_response(packet)

        self.assertEqual(message["event"], 152)
        self.assertEqual(message["payload"], {"status": "ok"})


class FakeWebSocket:
    """记录 TTS 客户端发送的二进制包。"""

    def __init__(self):
        """创建空的已发送数据列表。"""
        self.sent = []

    async def send(self, packet):
        """保存一条客户端发送的数据包。"""
        self.sent.append(packet)


class PersistentFakeWebSocket(FakeWebSocket):
    """模拟可以握手、保持连接并最终关闭的 WebSocket。"""

    def __init__(self):
        """准备 ConnectionStarted 响应和打开状态。"""
        super().__init__()
        self.state = State.OPEN
        self.responses = [event_packet(0x94, 50, "connection", b"{}")]

    async def recv(self):
        """返回下一条预置服务端响应。"""
        return self.responses.pop(0)

    async def close(self):
        """模拟关闭持久连接。"""
        self.state = State.CLOSED


class TTSStreamingTests(unittest.IsolatedAsyncioTestCase):
    async def test_sends_text_chunks_then_finishes_session(self):
        """验证文本流逐块发送，并以 FinishSession 事件结束。"""
        async def text_chunks():
            """生成两块 LLM 增量文本。"""
            yield "你好，"
            yield "世界。"

        ws = FakeWebSocket()
        client = TTSClient("key", "seed-tts-2.0", "voice")
        session_ready = asyncio.Event()
        session_ready.set()
        await client._send_text_stream(
            ws, "session", text_chunks(), session_ready
        )

        events = [struct.unpack_from(">i", packet, 4)[0] for packet in ws.sent]
        self.assertEqual(events, [200, 200, 102])

    async def test_prefetches_text_while_session_is_starting(self):
        """验证 Session 握手期间已开始获取 LLM 首段文本。"""
        text_requested = asyncio.Event()
        session_ready = asyncio.Event()

        async def text_chunks():
            text_requested.set()
            yield "你好"

        ws = FakeWebSocket()
        client = TTSClient("key", "seed-tts-2.0", "voice")
        sender = asyncio.create_task(
            client._send_text_stream(
                ws, "session", text_chunks(), session_ready
            )
        )

        await asyncio.wait_for(text_requested.wait(), timeout=1)
        self.assertEqual(ws.sent, [])

        session_ready.set()
        await sender
        events = [struct.unpack_from(">i", packet, 4)[0] for packet in ws.sent]
        self.assertEqual(events, [200, 102])

    async def test_reuses_one_connection_until_close(self):
        """验证重复连接请求复用同一 WebSocket，并只握手一次。"""
        ws = PersistentFakeWebSocket()
        connect = AsyncMock(return_value=ws)
        client = TTSClient("key", "seed-tts-2.0", "voice")

        with patch("vvc.direct.tts_client.websockets.connect", connect):
            await client.connect()
            await client.connect()
            await client.close()

        self.assertEqual(connect.await_count, 1)
        events = [struct.unpack_from(">i", packet, 4)[0] for packet in ws.sent]
        self.assertEqual(events, [1, 2])


class ASRClientTests(unittest.TestCase):
    def test_request_enables_speaker_diarization_and_server_vad(self):
        """验证 ASR 请求启用了说话人分离和 800 ms 服务端判停。"""
        packet = ASRClient("key", "resource").build_full_client_request()
        payload_size = struct.unpack_from(">I", packet, 4)[0]
        payload = gzip.decompress(packet[8:8 + payload_size])
        request = json.loads(payload.decode("utf-8"))["request"]
        self.assertEqual(request["result_type"], "single")

        self.assertTrue(request["show_utterances"])
        self.assertTrue(request["enable_speaker_info"])
        self.assertEqual(request["ssd_version"], "200")
        self.assertTrue(request["enable_nonstream"])
        self.assertEqual(request["end_window_size"], 800)
        self.assertEqual(request["force_to_speech_time"], 0)

class PersistentASRSendingTests(unittest.IsolatedAsyncioTestCase):
    async def test_sends_silence_while_listening_is_paused(self):
        """验证播报期间仍向 ASR 发送等长静音包以维持音频流。"""
        class RecorderStub:
            async def get_audio_chunk(self):
                await asyncio.sleep(0)
                return b"\x01\x02\x03\x04"

        class ASRStub:
            def __init__(self):
                self.chunks = []
                self.sent = asyncio.Event()

            async def send_audio(self, chunk):
                self.chunks.append(chunk)
                self.sent.set()

        recorder = RecorderStub()
        asr = ASRStub()
        listening_enabled = asyncio.Event()
        task = asyncio.create_task(
            send_audio_loop(recorder, asr, listening_enabled)
        )
        try:
            await asyncio.wait_for(asr.sent.wait(), timeout=1)
            self.assertEqual(asr.chunks, [b"\x00\x00\x00\x00"])
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_processes_sequential_incremental_utterances(self):
        """验证 single 模式会依次处理服务端返回的新分句。"""
        first = {
            "text": "你好",
            "definite": True,
            "start_time": 0,
            "end_time": 500,
        }
        second = {
            "text": "你好",
            "definite": True,
            "start_time": 3000,
            "end_time": 3500,
        }

        class ASRStub:
            def __init__(self):
                self.results = [
                    {"result": {"text": "你好", "utterances": [first]}},
                    {"result": {"text": "你好", "utterances": [second]}},
                ]
                self.finished = asyncio.Event()

            async def receive(self):
                if self.results:
                    return self.results.pop(0)
                await self.finished.wait()

        queue = asyncio.Queue()
        transcript = []
        task = asyncio.create_task(
            receive_final_texts(
                ASRStub(), queue, transcript, accepting_results()
            )
        )
        try:
            first_text = await asyncio.wait_for(queue.get(), timeout=1)
            second_text = await asyncio.wait_for(queue.get(), timeout=1)
            await asyncio.sleep(0)

            self.assertEqual(
                [first_text, second_text],
                [("未知", "你好"), ("未知", "你好")],
            )
            self.assertTrue(queue.empty())
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_displays_only_current_partial_utterance(self):
        """验证终端只显示当前未确认分句。"""
        current = {
            "text": "第二轮正在说",
            "definite": False,
            "start_time": 3000,
            "end_time": 3400,
        }

        class ASRStub:
            def __init__(self):
                self.returned = False
                self.finished = asyncio.Event()

            async def receive(self):
                if not self.returned:
                    self.returned = True
                    return {
                        "result": {
                            "text": "第二轮正在说",
                            "utterances": [current],
                        }
                    }
                await self.finished.wait()

        queue = asyncio.Queue()
        transcript = []
        with patch("builtins.print") as output:
            task = asyncio.create_task(
                receive_final_texts(
                    ASRStub(), queue, transcript, accepting_results()
                )
            )
            try:
                await asyncio.sleep(0)
                await asyncio.sleep(0)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        displayed = "".join(
            str(call.args[0]) for call in output.call_args_list if call.args
        )
        self.assertIn("识别结果：第二轮正在说", displayed)
        self.assertNotIn("第一轮内容第二轮正在说", displayed)

    async def test_displays_speaker_for_final_utterance(self):
        """验证定稿分句带有说话人标识。"""
        utterance = {
            "text": "这个方案可以。",
            "definite": True,
            "additions": {"speaker_id": "1"},
        }

        class ASRStub:
            def __init__(self):
                self.returned = False
                self.finished = asyncio.Event()

            async def receive(self):
                if not self.returned:
                    self.returned = True
                    return {"result": {"utterances": [utterance]}}
                await self.finished.wait()

        queue = asyncio.Queue()
        transcript = []
        with patch("builtins.print") as output:
            task = asyncio.create_task(
                receive_final_texts(
                    ASRStub(), queue, transcript, accepting_results()
                )
            )
            try:
                self.assertEqual(
                    await asyncio.wait_for(queue.get(), timeout=1),
                    ("1", "这个方案可以。"),
                )
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        self.assertEqual(transcript, ["这个方案可以。"])
        output.assert_any_call(
            "\r\033[2K识别结果：这个方案可以。",
            end="",
            flush=True,
        )

    async def test_ignores_late_results_while_replying(self):
        """验证播报期间的迟到识别结果不会进入下一轮对话。"""
        utterance = {
            "text": "迟到的分句。",
            "definite": True,
            "additions": {"speaker_id": "1"},
        }

        class ASRStub:
            def __init__(self):
                self.returned = False
                self.finished = asyncio.Event()

            async def receive(self):
                if not self.returned:
                    self.returned = True
                    return {"result": {"utterances": [utterance]}}
                await self.finished.wait()

        queue = asyncio.Queue()
        transcript = []
        accepting = asyncio.Event()
        task = asyncio.create_task(
            receive_final_texts(ASRStub(), queue, transcript, accepting)
        )
        try:
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.assertTrue(queue.empty())
            self.assertEqual(transcript, [])
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_collects_all_utterances_before_replying(self):
        """验证静音窗口内的多人发言会合并为同一轮对话。"""
        class RecorderStub:
            def __init__(self):
                self.last_speech_time = asyncio.get_running_loop().time()
                self.speech_detected = asyncio.Event()

        queue = asyncio.Queue()
        await queue.put(("0", "世界真小。"))
        await queue.put(("1", "我坐飞机也遇到了熟人。"))

        conversation = await collect_conversation(
            queue, RecorderStub(), pause_seconds=0.01
        )

        self.assertEqual(
            conversation,
            [
                ("0", "世界真小。"),
                ("1", "我坐飞机也遇到了熟人。"),
            ],
        )

    async def test_recorder_marks_loud_audio_as_speech(self):
        """验证本地 VAD 仅在音频能量超过阈值时记录人声。"""
        recorder = Recorder(vad_threshold=500)
        recorder.loop = asyncio.get_running_loop()
        recorder._record_chunk(b"\x00\x00" * 1600)
        self.assertFalse(recorder.speech_detected.is_set())

        recorder._record_chunk(struct.pack("<h", 1000) * 1600)
        self.assertTrue(recorder.speech_detected.is_set())

class FakeOutputStream:
    instances = []

    def __init__(self, **kwargs):
        """记录播放器传入的配置和音频块。"""
        self.kwargs = kwargs
        self.writes = []
        self.stopped = False
        self.closed = False
        self.instances.append(self)

    def start(self):
        """模拟启动声卡输出流。"""
        pass

    def write(self, chunk):
        """保存播放器写入的音频块。"""
        self.writes.append(chunk)

    def stop(self):
        """标记输出流已经停止。"""
        self.stopped = True

    def close(self):
        """标记输出流已经关闭。"""
        self.closed = True


class AudioPlayerTests(unittest.IsolatedAsyncioTestCase):
    async def test_streams_all_chunks_and_waits_for_stop(self):
        """验证播放器消费全部音频、补齐尾部静音并完成关闭。"""
        async def chunks():
            """生成两块测试音频。"""
            yield b"a"
            await asyncio.sleep(0)
            yield b"b"

        FakeOutputStream.instances.clear()
        with patch(
            "vvc.direct.audio_player.sd.RawOutputStream", FakeOutputStream
        ):
            await AudioPlayer(
                sample_rate=1000,
                tail_padding_seconds=0.1,
            ).play_stream(chunks())

        stream = FakeOutputStream.instances[0]
        self.assertEqual(stream.writes[:2], [b"a", b"b"])
        self.assertEqual(stream.writes[2], bytes(200))
        self.assertTrue(stream.stopped)
        self.assertTrue(stream.closed)

    async def test_can_disable_tail_padding(self):
        """验证不需要设备收尾缓冲时可以关闭静音填充。"""
        async def chunks():
            yield b"pcm"

        FakeOutputStream.instances.clear()
        with patch(
            "vvc.direct.audio_player.sd.RawOutputStream", FakeOutputStream
        ):
            await AudioPlayer(tail_padding_seconds=0).play_stream(chunks())

        self.assertEqual(FakeOutputStream.instances[0].writes, [b"pcm"])


if __name__ == "__main__":
    unittest.main()
