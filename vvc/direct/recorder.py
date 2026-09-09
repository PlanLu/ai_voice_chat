import asyncio
import sounddevice as sd


class Recorder:
    def __init__(
        self, sample_rate=16000, channels=1, block_size=1600, vad_threshold=500
    ):
        """保存录音格式并创建跨线程音频队列。"""
        self.sample_rate = sample_rate
        self.channels = channels
        self.block_size = block_size

        self.audio_queue = asyncio.Queue(maxsize=20)
        self.vad_threshold = vad_threshold
        self.last_speech_time = 0
        self.speech_detected = asyncio.Event()
        self.stream = None
        self.loop = None

    def start(self):
        """清空旧音频并启动麦克风输入流。"""
        self.clear_queue()

        # 获取当前 asyncio 事件循环
        self.loop = asyncio.get_running_loop()

        def callback(indata, frames, time, status):
            """把声卡线程产生的 PCM 数据安全送回 asyncio 事件循环。"""
            if status:
                print(status)

            chunk = bytes(indata)

            # callback 在别的线程中，所以要这样安全地放入 asyncio.Queue
            self.loop.call_soon_threadsafe(self._record_chunk, chunk)

        self.stream = sd.RawInputStream(
            samplerate=self.sample_rate,
            channels=self.channels,
            dtype="int16",
            blocksize=self.block_size,
            callback=callback
        )

        self.stream.start()
        print("开始录音")

    async def get_audio_chunk(self):
        """等待并返回下一块麦克风 PCM 数据。"""
        return await self.audio_queue.get()

    def _record_chunk(self, chunk):
        """记录本地 VAD 状态，并将音频无阻塞入队。"""
        samples = memoryview(chunk).cast("h")
        energy = sum(sample * sample for sample in samples) / len(samples)
        if energy >= self.vad_threshold * self.vad_threshold:
            self.last_speech_time = self.loop.time()
            self.speech_detected.set()

        try:
            self.audio_queue.put_nowait(chunk)
        except asyncio.QueueFull:
            # 事件循环短暂繁忙时丢弃一块，避免录音回调线程报错。
            pass

    def clear_queue(self):
        """清空上一轮尚未消费的录音数据。"""
        while not self.audio_queue.empty():
            self.audio_queue.get_nowait()

    def stop(self):
        """关闭麦克风输入流并清理残留音频。"""
        if self.stream:
            self.stream.stop()
            self.stream.close()
            self.stream = None

        self.clear_queue()
