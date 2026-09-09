const TARGET_SAMPLE_RATE = 16000;

function encodeWav(samples, sampleRate) {
  const buffer = new ArrayBuffer(44 + samples.length * 2);
  const view = new DataView(buffer);
  const writeText = (offset, text) => [...text]
    .forEach((char, index) => view.setUint8(offset + index, char.charCodeAt(0)));
  writeText(0, 'RIFF');
  view.setUint32(4, 36 + samples.length * 2, true);
  writeText(8, 'WAVE');
  writeText(12, 'fmt ');
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  writeText(36, 'data');
  view.setUint32(40, samples.length * 2, true);
  samples.forEach((sample, index) => {
    const value = Math.max(-1, Math.min(1, sample));
    view.setInt16(44 + index * 2, value < 0 ? value * 0x8000 : value * 0x7fff, true);
  });
  return new Uint8Array(buffer);
}

function resampleTo16k(samples, sourceRate) {
  if (sourceRate === TARGET_SAMPLE_RATE) return samples;
  const targetLength = Math.floor(samples.length * TARGET_SAMPLE_RATE / sourceRate);
  const result = new Float32Array(targetLength);
  const ratio = sourceRate / TARGET_SAMPLE_RATE;
  for (let index = 0; index < targetLength; index += 1) {
    const sourceIndex = index * ratio;
    const lower = Math.floor(sourceIndex);
    const upper = Math.min(lower + 1, samples.length - 1);
    const fraction = sourceIndex - lower;
    result[index] = samples[lower] * (1 - fraction) + samples[upper] * fraction;
  }
  return result;
}

function toBase64(bytes) {
  let binary = '';
  const chunkSize = 0x8000;
  for (let index = 0; index < bytes.length; index += chunkSize) {
    binary += String.fromCharCode(...bytes.subarray(index, index + chunkSize));
  }
  return btoa(binary);
}

function volumeLevel(samples) {
  let energy = 0;
  for (const sample of samples) energy += sample * sample;
  const rms = Math.sqrt(energy / Math.max(samples.length, 1));
  return Math.min(100, Math.round(rms * 450));
}

export class VoiceprintRecorder {
  constructor(durationSeconds = 20) {
    this.durationSeconds = durationSeconds;
    this.finish = undefined;
  }

  get active() {
    return Boolean(this.finish);
  }

  cancel() {
    this.finish?.(true);
  }

  async record(onProgress) {
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: false,
        noiseSuppression: false,
        autoGainControl: false,
      },
    });
    const audioContext = new AudioContext({ sampleRate: TARGET_SAMPLE_RATE });
    const source = audioContext.createMediaStreamSource(stream);
    const processor = audioContext.createScriptProcessor(4096, 1, 1);
    const silentOutput = audioContext.createGain();
    silentOutput.gain.value = 0;
    const samples = [];
    const startedAt = performance.now();
    let timer;
    let finished = false;

    processor.onaudioprocess = (event) => {
      const chunk = new Float32Array(event.inputBuffer.getChannelData(0));
      samples.push(chunk);
      const elapsed = Math.min(
        this.durationSeconds,
        (performance.now() - startedAt) / 1000
      );
      onProgress?.({
        elapsed,
        remaining: Math.max(0, this.durationSeconds - elapsed),
        volume: volumeLevel(chunk),
      });
    };
    source.connect(processor);
    processor.connect(silentOutput);
    silentOutput.connect(audioContext.destination);

    return new Promise((resolve) => {
      const finish = async (cancelled) => {
        if (finished) return;
        finished = true;
        clearTimeout(timer);
        processor.disconnect();
        silentOutput.disconnect();
        source.disconnect();
        stream.getTracks().forEach((track) => track.stop());
        const sampleRate = audioContext.sampleRate;
        await audioContext.close();
        this.finish = undefined;
        if (cancelled) {
          resolve(null);
          return;
        }
        const totalLength = samples.reduce(
          (total, chunk) => total + chunk.length,
          0
        );
        const merged = new Float32Array(totalLength);
        let offset = 0;
        samples.forEach((chunk) => {
          merged.set(chunk, offset);
          offset += chunk.length;
        });
        resolve(toBase64(encodeWav(resampleTo16k(merged, sampleRate), TARGET_SAMPLE_RATE)));
      };
      this.finish = finish;
      timer = setTimeout(() => finish(false), this.durationSeconds * 1000);
    });
  }
}
