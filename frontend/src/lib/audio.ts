// Audio-Pipeline der Web-UI:
// - MicCapture: getUserMedia -> AudioWorklet -> Downsampling auf PCM16/16k
//   (Whisper-Eingang laut Protokoll), ~128-ms-Chunks + RMS-Pegel fuer VAD
// - StreamPlayer: PCM16-Chunks des Servers (typisch 24k XTTS) lueckenlos
//   ueber WebAudio abspielen; stop() = Barge-in

const TARGET_RATE = 16000;
const CHUNK_SAMPLES = 2048; // @16k ≈ 128 ms

// Inline-Worklet: schiebt rohe Float32-Bloecke an den Main-Thread. Das
// Downsampling passiert dort - haelt den Worklet trivial.
const WORKLET_SOURCE = `
class MicTap extends AudioWorkletProcessor {
  process(inputs) {
    const channel = inputs[0]?.[0];
    if (channel) this.port.postMessage(channel.slice(0));
    return true;
  }
}
registerProcessor("mic-tap", MicTap);
`;

function downsample(input: Float32Array, fromRate: number, toRate: number): Float32Array {
  if (fromRate === toRate) return input;
  const ratio = fromRate / toRate;
  const output = new Float32Array(Math.floor(input.length / ratio));
  for (let i = 0; i < output.length; i++) {
    // Lineare Interpolation reicht fuer Sprache voellig.
    const pos = i * ratio;
    const left = Math.floor(pos);
    const right = Math.min(left + 1, input.length - 1);
    const frac = pos - left;
    output[i] = input[left] * (1 - frac) + input[right] * frac;
  }
  return output;
}

export class MicCapture {
  private context: AudioContext | null = null;
  private stream: MediaStream | null = null;
  private buffer: number[] = [];
  private onChunk: (pcm: Int16Array) => void;
  private onLevel?: (rms: number) => void;

  constructor(onChunk: (pcm: Int16Array) => void, onLevel?: (rms: number) => void) {
    this.onChunk = onChunk;
    this.onLevel = onLevel;
  }

  get active(): boolean {
    return this.context !== null;
  }

  async start(): Promise<void> {
    if (this.context) return;
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true },
    });
    this.context = new AudioContext();
    const workletUrl = URL.createObjectURL(
      new Blob([WORKLET_SOURCE], { type: "application/javascript" }),
    );
    await this.context.audioWorklet.addModule(workletUrl);
    URL.revokeObjectURL(workletUrl);

    const source = this.context.createMediaStreamSource(this.stream);
    const tap = new AudioWorkletNode(this.context, "mic-tap");
    tap.port.onmessage = (event) => this.handleBlock(event.data as Float32Array);
    source.connect(tap);
  }

  private handleBlock(block: Float32Array): void {
    if (!this.context) return;
    const resampled = downsample(block, this.context.sampleRate, TARGET_RATE);

    let sum = 0;
    for (const sample of resampled) sum += sample * sample;
    this.onLevel?.(Math.sqrt(sum / resampled.length));

    for (const sample of resampled) this.buffer.push(sample);
    while (this.buffer.length >= CHUNK_SAMPLES) {
      const chunk = this.buffer.splice(0, CHUNK_SAMPLES);
      const pcm = new Int16Array(CHUNK_SAMPLES);
      for (let i = 0; i < CHUNK_SAMPLES; i++) {
        pcm[i] = Math.max(-32768, Math.min(32767, Math.round(chunk[i] * 32767)));
      }
      this.onChunk(pcm);
    }
  }

  stop(): void {
    this.stream?.getTracks().forEach((track) => track.stop());
    this.context?.close();
    this.context = null;
    this.stream = null;
    this.buffer = [];
  }
}

export class StreamPlayer {
  private context: AudioContext | null = null;
  private nextTime = 0;
  private sources = new Set<AudioBufferSourceNode>();
  private onStateChange?: (speaking: boolean) => void;

  constructor(onStateChange?: (speaking: boolean) => void) {
    this.onStateChange = onStateChange;
  }

  get speaking(): boolean {
    return this.sources.size > 0;
  }

  play(pcm: Int16Array, sampleRate: number): void {
    if (pcm.length === 0) return;
    if (!this.context) this.context = new AudioContext();
    const context = this.context;

    const buffer = context.createBuffer(1, pcm.length, sampleRate);
    const channel = buffer.getChannelData(0);
    for (let i = 0; i < pcm.length; i++) channel[i] = pcm[i] / 32768;

    const source = context.createBufferSource();
    source.buffer = buffer;
    source.connect(context.destination);

    // Chunks nahtlos hintereinander planen (kleiner Vorlauf beim Start).
    const now = context.currentTime;
    if (this.nextTime < now) this.nextTime = now + 0.05;
    source.start(this.nextTime);
    this.nextTime += buffer.duration;

    if (this.sources.size === 0) this.onStateChange?.(true);
    this.sources.add(source);
    source.onended = () => {
      this.sources.delete(source);
      if (this.sources.size === 0) this.onStateChange?.(false);
    };
  }

  /** Barge-in: alles Geplante sofort verwerfen. */
  stop(): void {
    for (const source of this.sources) {
      source.onended = null;
      try {
        source.stop();
      } catch {
        /* bereits gestoppt */
      }
    }
    if (this.sources.size > 0) this.onStateChange?.(false);
    this.sources.clear();
    this.nextTime = 0;
  }
}

/** Einfache Energie-VAD fuer den Realtime Talk: meldet Sprechbeginn und
 *  -ende (nach silenceMs Stille). Schwellwert bewusst konservativ - im
 *  Zweifel lieber etwas laenger aufnehmen. */
export class VoiceActivity {
  speaking = false;
  private lastVoiceAt = 0;
  private threshold: number;
  private silenceMs: number;

  constructor(threshold = 0.015, silenceMs = 900) {
    this.threshold = threshold;
    this.silenceMs = silenceMs;
  }

  /** Liefert "start" | "end" | null fuer jeden Pegelwert. */
  update(rms: number): "start" | "end" | null {
    const now = performance.now();
    if (rms >= this.threshold) {
      this.lastVoiceAt = now;
      if (!this.speaking) {
        this.speaking = true;
        return "start";
      }
      return null;
    }
    if (this.speaking && now - this.lastVoiceAt > this.silenceMs) {
      this.speaking = false;
      return "end";
    }
    return null;
  }

  reset(): void {
    this.speaking = false;
    this.lastVoiceAt = 0;
  }
}
