import asyncio

import sounddevice as sd


class AudioPlayer:
    def __init__(
        self,
        sample_rate=24000,
        channels=1,
        queue_size=20,
        tail_padding_seconds=0.3,
    ):
        """保存声卡输出格式和流式缓冲队列大小。"""
        self.sample_rate = sample_rate
        self.channels = channels
        self.queue_size = queue_size
        self.tail_padding_seconds = tail_padding_seconds

    async def play_stream(self, audio_stream):
        """边接收 PCM 音频边播放，直到服务端和声卡都处理完毕。"""
        queue = asyncio.Queue(maxsize=self.queue_size)
        end_marker = object()

        async def receive_audio():
            """把网络返回的音频块放入有界播放队列。"""
            try:
                async for chunk in audio_stream:
                    await queue.put(chunk)
            finally:
                await queue.put(end_marker)

        async def play_audio():
            """从队列读取 PCM 数据并写入声卡。"""
            stream = sd.RawOutputStream(
                samplerate=self.sample_rate,
                channels=self.channels,
                dtype="int16",
            )
            stream.start()
            try:
                while True:
                    chunk = await queue.get()
                    if chunk is end_marker:
                        break
                    await asyncio.to_thread(stream.write, chunk)

                # 部分声卡驱动在 stop/close 时会裁掉仍停留在设备缓冲区的
                # 最后一小段音频。写入短静音把实际语音推出硬件缓冲，避免
                # 回复末尾的一两个字没有播放完整。
                padding_frames = round(
                    self.sample_rate * self.tail_padding_seconds
                )
                if padding_frames > 0:
                    silence = bytes(padding_frames * self.channels * 2)
                    await asyncio.to_thread(stream.write, silence)
                await asyncio.to_thread(stream.stop)
            finally:
                stream.close()

        producer = asyncio.create_task(receive_audio())
        consumer = asyncio.create_task(play_audio())
        try:
            await asyncio.gather(producer, consumer)
        finally:
            for task in (producer, consumer):
                if not task.done():
                    task.cancel()
            await asyncio.gather(producer, consumer, return_exceptions=True)
