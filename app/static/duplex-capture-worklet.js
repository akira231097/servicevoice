// Capture only. Never feed microphone samples to the loudspeaker.
class ServiceVoiceDuplexCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.epoch = 0; this.muted = false;
    this.port.onmessage = event => {
      if (event.data?.type === 'epoch' && Number.isSafeInteger(event.data.epoch) && event.data.epoch >= this.epoch) {
        this.epoch = event.data.epoch; this.muted = event.data.muted === true;
      }
    };
  }
  process(inputs, outputs) {
    for (const output of outputs) for (const channel of output) channel.fill(0);
    const channel = inputs[0]?.[0];
    if (channel?.length) {
      const samples = this.muted ? new Float32Array(channel.length) : new Float32Array(channel);
      this.port.postMessage({ samples, epoch: this.epoch }, [samples.buffer]);
    }
    return true;
  }
}
registerProcessor('servicevoice-duplex-capture', ServiceVoiceDuplexCapture);
