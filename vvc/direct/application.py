import asyncio
import os

from dotenv import load_dotenv

from vvc.direct.asr_client import ASRClient
from vvc.direct.audio_player import AudioPlayer
from vvc.direct.llm_client import LLMClient
from vvc.direct.recorder import Recorder
from vvc.direct.tts_client import TTSClient


load_dotenv()

SYSTEM_PROMPT = (
    "你是一个自然、友好的中文语音助手。回答要非常的简短，亲切"
    "除非用户明确要求，否则避免 Markdown、长列表和冗长解释。"
)
TTS_SAMPLE_RATE = 24000
CONVERSATION_PAUSE_SECONDS = 0


def require_env(name):
    """读取必需的环境变量，缺失时给出明确错误。"""
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"缺少环境变量: {name}")
    return value


async def send_audio_loop(recorder, asr, listening_enabled):
    """持续上传 PCM；播报期间以静音包保活，避免识别到助手声音。"""
    while True:
        chunk = await recorder.get_audio_chunk()
        if not listening_enabled.is_set():
            # ASR 是持续音频流。不能在 TTS 播放期间停止发包，否则服务端
            # 可能将长时间无输入视为流结束。发送等长静音可保持连接存活。
            chunk = bytes(len(chunk))
        await asr.send_audio(chunk)


async def receive_final_texts(
    asr, user_text_queue, transcript, accepting_results
):
    """实时显示识别文本，并将确认的分句交给对话收集器。"""
    while True:
        result = await asr.receive()
        if not result:
            continue
        if not accepting_results.is_set():
            continue

        asr_result = result.get("result") or {}
        utterances = asr_result.get("utterances", [])
        if utterances:
            current = utterances[-1]
            current_text = current.get("text", "").strip()
            if current_text and current.get("definite") is not True:
                print(
                    f"\r\033[2K识别结果：{' '.join(transcript + [current_text])}",
                    end="",
                    flush=True,
                )

        for utterance in utterances:
            if utterance.get("definite") is True:
                final_text = utterance.get("text", "").strip()
                if final_text:
                    speaker_id = utterance.get("additions", {}).get(
                        "speaker_id", "未知"
                    )
                    transcript.append(final_text)
                    print(
                        f"\r\033[2K识别结果：{' '.join(transcript)}",
                        end="",
                        flush=True,
                    )
                    await user_text_queue.put((speaker_id, final_text))


async def collect_conversation(
    user_text_queue, recorder, pause_seconds=CONVERSATION_PAUSE_SECONDS
):
    """收集连续对话，直到本地 VAD 检测到指定时长静音。"""
    conversation = [await user_text_queue.get()]
    recorder.speech_detected.clear()
    while True:
        timeout = (
            recorder.last_speech_time
            + pause_seconds
            - asyncio.get_running_loop().time()
        )
        next_utterance = asyncio.create_task(user_text_queue.get())
        speech_detected = asyncio.create_task(recorder.speech_detected.wait())
        done, pending = await asyncio.wait(
            (next_utterance, speech_detected),
            timeout=max(timeout, 0),
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

        if next_utterance in done:
            conversation.append(next_utterance.result())
        elif speech_detected in done:
            recorder.speech_detected.clear()
        else:
            return conversation


async def display_reply_stream(llm, messages, reply_parts):
    """显示 LLM 增量回复、保存完整文本，并把相同文本流交给 TTS。"""
    async for text in llm.stream(messages):
        print(text, end="", flush=True)
        reply_parts.append(text)
        yield text


async def main():
    """初始化各模块，并循环执行识别、生成、合成和播放。"""
    speech_api_key = require_env("DOUBAO_SPEECH_API_KEY")
    asr_resource_id = require_env("DOUBAO_SPEECH_RESOURCE_ID")
    tts_resource_id = require_env("DOUBAO_TTS_RESOURCE_ID")
    tts_voice_type = require_env("DOUBAO_TTS_VOICE_TYPE")
    require_env("LLM_MODEL")
    require_env("OPENAI_API_KEY")

    recorder = Recorder()
    asr = ASRClient(
        api_key=speech_api_key,
        resource_id=asr_resource_id,
        hotword_table_id=os.getenv("DOUBAO_ASR_HOTWORD_TABLE_ID"),
    )
    llm = LLMClient()
    tts = TTSClient(
        api_key=speech_api_key,
        resource_id=tts_resource_id,
        voice_type=tts_voice_type,
        sample_rate=TTS_SAMPLE_RATE,
    )
    player = AudioPlayer(sample_rate=TTS_SAMPLE_RATE)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    listening_enabled = asyncio.Event()
    listening_enabled.set()
    accepting_results = asyncio.Event()
    accepting_results.set()
    user_text_queue = asyncio.Queue()
    transcript = []
    background_tasks = []

    try:
        await asr.connect()
        recorder.start()
        background_tasks = [
            asyncio.create_task(
                send_audio_loop(recorder, asr, listening_enabled)
            ),
            asyncio.create_task(
                receive_final_texts(
                    asr, user_text_queue, transcript, accepting_results
                )
            ),
        ]

        try:
            await tts.connect()
        except Exception as exc:
            print(f"TTS预连接失败，将在回复时重试：{exc}")

        print("语音助手已启动，按 Ctrl+C 退出。")
        print("请开始说话...")

        while True:
            try:
                conversation = await collect_conversation(user_text_queue, recorder)
                accepting_results.clear()
                print()
                for speaker_id, text in conversation:
                    print(f"说话人 {speaker_id}：{text}")

                user_text = "\n".join(
                    f"说话人 {speaker_id}：{text}"
                    for speaker_id, text in conversation
                )
                # 对话结束后再切换为静音上传，隔离播放音。
                listening_enabled.clear()
                recorder.clear_queue()

                print("思考中...")
                pending_messages = messages + [
                    {"role": "user", "content": user_text}
                ]
                reply_parts = []

                try:
                    print("助手：", end="", flush=True)
                    text_stream = display_reply_stream(
                        llm, pending_messages, reply_parts
                    )
                    await player.play_stream(tts.stream(text_stream))
                    print()
                finally:
                    # 丢弃暂停期间残留的录音，再开始监听下一轮用户输入。
                    recorder.clear_queue()
                    transcript.clear()
                    accepting_results.set()
                    listening_enabled.set()

                reply = "".join(reply_parts).strip()
                if not reply:
                    raise RuntimeError("大模型返回了空回复")

                messages.extend(
                    [
                        {"role": "user", "content": user_text},
                        {"role": "assistant", "content": reply},
                    ]
                )
                print("播报结束。\n")
                print("请继续说话...")
            except Exception as exc:
                print(f"\n本轮失败：{exc}\n")
    finally:
        recorder.stop()
        for task in background_tasks:
            task.cancel()
        await asyncio.gather(*background_tasks, return_exceptions=True)
        await asr.close()
        await tts.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n语音助手已退出。")
