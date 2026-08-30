class Pcm16Resampler extends AudioWorkletProcessor {
  constructor() {
    super();
    this.targetSampleRate = 16000;
    this.sourceSampleRate = sampleRate;
    this.ratio = this.targetSampleRate / this.sourceSampleRate;
    this.buffer = [];
    this.frameSize = 320; // 20 ms at 16 kHz, mono
    this.lastValue = 0;
  }

  process(inputs, outputs, parameters) {
    const input = inputs[0];
    if (!input || !input.length) return true;
    const channel = input[0];

    for (let i = 0; i < channel.length; i++) {
      this.buffer.push(channel[i]);
    }

    // Simple linear resampling to 16 kHz
    const needed = Math.floor(this.frameSize / this.ratio);
    while (this.buffer.length >= needed + 1) {
      const frame = new Int16Array(this.frameSize);
      for (let n = 0; n < this.frameSize; n++) {
        const srcIdx = n / this.ratio;
        const i0 = Math.floor(srcIdx);
        const i1 = Math.min(i0 + 1, this.buffer.length - 1);
        const frac = srcIdx - i0;
        const v = this.buffer[i0] * (1 - frac) + this.buffer[i1] * frac;
        frame[n] = Math.max(-1, Math.min(1, v)) * 0x7fff;
      }
      this.port.postMessage(frame.buffer, [frame.buffer]);
      const consume = Math.floor(this.frameSize / this.ratio);
      this.buffer.splice(0, consume);
    }

    return true;
  }
}

registerProcessor('pcm16-resampler', Pcm16Resampler);
