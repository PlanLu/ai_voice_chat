import argparse
import io
import os
import time
import wave

import sounddevice as sd
from dotenv import load_dotenv

from vvc.rtc.api import RTCAPI
from vvc.shared.voiceprint_registry import VoiceprintRegistry


SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH = 2


def record_wav(seconds):
    """从默认麦克风录制单声道 16 kHz PCM，并封装成 WAV。"""
    chunks = []

    def callback(indata, frames, callback_time, status):
        if status:
            print(status)
        chunks.append(bytes(indata))

    print(f"请开始连续说话，将录制 {seconds} 秒……")
    with sd.RawInputStream(
        samplerate=SAMPLE_RATE,
        channels=CHANNELS,
        dtype="int16",
        callback=callback,
    ):
        time.sleep(seconds)

    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(CHANNELS)
        wav.setsampwidth(SAMPLE_WIDTH)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(b"".join(chunks))
    return output.getvalue()


def require_env(name):
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"缺少环境变量: {name}")
    return value


def main():
    parser = argparse.ArgumentParser(description="录制并注册一个长期声纹")
    parser.add_argument("name", help="人员姓名，例如：张三")
    parser.add_argument("--seconds", type=int, default=20, help="录制秒数")
    args = parser.parse_args()

    if not 15 <= args.seconds <= 30:
        raise ValueError("声纹注册录音应为 15～30 秒")

    load_dotenv()
    client = RTCAPI(
        require_env("VOLC_ACCESS_KEY"),
        require_env("VOLC_SECRET_KEY"),
    )
    voiceprint_id = client.register_voiceprint(
        require_env("RTC_APP_ID"),
        args.name,
        record_wav(args.seconds),
    )
    VoiceprintRegistry().save(args.name, voiceprint_id)
    print(f"注册成功：{args.name} -> {voiceprint_id}")


if __name__ == "__main__":
    main()
