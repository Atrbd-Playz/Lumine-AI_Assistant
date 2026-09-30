/**
 * Lumine's voice, measured.
 *
 * ## What this is for
 *
 * The avatar used to be animated entirely on a clock, which meant it was
 * *equally* animated whether Lumine was mid-sentence or silent. That is the
 * difference between a character and a screensaver: a face that keeps gesturing
 * through a two-second pause is gesturing at nobody.
 *
 * This measures the remote audio track's loudness and publishes it, so the
 * presence layer can move in response to what she is actually saying.
 *
 * ## Why a subscription and not a polled value
 *
 * The natural alternative is a `getLevel()` on a snapshot that the UI reads on an
 * interval. That is a render per tick to move a silhouette, and it caps the
 * refresh at whatever the interval is — so the animation is either expensive or
 * low-resolution. A push subscription lets the value be delivered at frame rate
 * and, crucially, lets the consumer store it without going through React state.
 *
 * ## Why RMS and not a peak
 *
 * A peak follower on speech spends most of its time at the same value: the
 * waveform's maximum is a property of the *format* (a TTS voice and a recorded
 * human voice both top out near full scale) rather than of the moment. RMS is
 * proportional to actual loudness, so it moves the way the sound does. The
 * `Math.sqrt` is over the *mean of squares*, which is the definition, not an
 * approximation of it.
 *
 * ## Why a noise floor exists
 *
 * Digital silence on a WebRTC track is not 0. It is the codec's comfort noise
 * and the room's own hum, and an un-gated meter sits permanently at a low
 * non-zero value that the body then treats as "she is speaking very quietly"
 * forever. The floor subtracts that, so silence reads as silence.
 *
 * ## The one thing this will not do
 *
 * It does not modify the audio. It taps the stream through its own graph branch
 * and never connects to the destination, so a level meter cannot be the reason
 * Lumine becomes inaudible.
 */

type LevelListener = (level: number) => void;

/**
 * The RMS below which we call it silence, as a fraction of full scale.
 *
 * A little above the comfort-noise floor rather than at it: the room between
 * "audible" and "loud enough to be speech" is real, and treating it as silence
 * is what makes a meter read as either stuck on or stuck off.
 */
const NOISE_FLOOR = 0.012;

/** What full scale maps to, after flooring. Headroom so normal speech is not pinned at 1. */
const HEADROOM = 0.28;

/**
 * How hard the reading is pulled toward the newest sample.
 *
 * Low on purpose. The engine smooths again on its own side, and a second
 * aggressive filter here would add lag to a signal whose entire value is being
 * in time with the word being spoken. This is only enough to take the edge off
 * frame-to-frame quantisation.
 */
const SMOOTHING = 0.35;

export class AudioLevelMonitor {
  private context: AudioContext | null = null;
  private analyser: AnalyserNode | null = null;
  private source: MediaStreamAudioSourceNode | null = null;
  private raf = 0;
  private readonly listeners = new Set<LevelListener>();
  private buffer: Float32Array<ArrayBuffer> | null = null;
  private level = 0;
  private closed = false;

  /** Subscribe to the smoothed level. Returns an unsubscribe. */
  subscribe(onLevel: LevelListener): () => void {
    this.listeners.add(onLevel);
    // Hand over the current value so a late subscriber is not stuck at 0 until the
    // next frame — which for a face that has just connected is a visible stall.
    onLevel(this.level);
    return () => {
      this.listeners.delete(onLevel);
    };
  }

  /**
   * Start measuring `stream`. Safe to call again; the previous graph is released.
   *
   * Re-calling matters because LiveKit re-subscribes tracks on reconnection, so
   * the same monitor outlives several tracks. Leaking a node per reconnect would
   * be invisible until the tab was slow.
   */
  attach(stream: MediaStream | undefined | null): void {
    this.detach();
    if (!stream || this.closed) return;

    // A 0-sample analyser throws on `getFloatTimeDomainData`, and a monitor that
    // throws in its own rAF stops the whole loop for the session.
    const AudioContextCtor = window.AudioContext ?? (window as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
    if (!AudioContextCtor) return;

    try {
      const context = new AudioContextCtor();
      const source = context.createMediaStreamSource(stream);
      const analyser = context.createAnalyser();
      analyser.fftSize = 1024;
      // No smoothing here on purpose: `AnalyserNode.smoothingTimeConstant` shapes
      // the frequency data, and we read the time domain, so it would do nothing
      // at all. The envelope is built from RMS below.
      source.connect(analyser);
      // Deliberately not connected to `context.destination`. A graph branch that
      // reaches the speakers would make the metering itself audible.

      this.context = context;
      this.analyser = analyser;
      this.source = source;
      this.buffer = new Float32Array(new ArrayBuffer(analyser.fftSize * Float32Array.BYTES_PER_ELEMENT));
      this.loop();
    } catch {
      // No Web Audio, or the stream has no live audio track. Audio-reactive motion
      // is an enhancement; the breath underneath it still runs, so a failure here
      // must not take the avatar with it.
      this.detach();
    }
  }

  /**
   * Stop measuring and release the graph.
   *
   * The `AudioContext` is explicitly closed, and that is the whole reason this is
   * a class with a `detach` rather than a bare rAF loop. An `AudioContext` is a
   * hard OS resource with its own thread and, on some platforms, a per-context
   * limit — a call that connects and ends a dozen times would leave a dozen of
   * them alive and eventually start refusing to create more. Nothing about the
   * leak announces itself; the app just gets quieter and then stops working.
   */
  detach(): void {
    if (this.raf) {
      window.cancelAnimationFrame(this.raf);
      this.raf = 0;
    }
    try {
      this.source?.disconnect();
      this.analyser?.disconnect();
    } catch {
      // Already detached, or the context is gone. Either way there is nothing to
      // salvage, and this must not prevent the close below.
    }
    this.source = null;
    this.analyser = null;
    this.buffer = null;
    this.level = 0;
    if (this.context) {
      void this.context.close().catch(() => undefined);
      this.context = null;
    }
  }

  /** Permanent teardown. Drops the listeners too, so a held callback cannot resurrect it. */
  close(): void {
    this.closed = true;
    this.detach();
    this.listeners.clear();
  }

  private loop = () => {
    if (this.closed || !this.analyser || !this.buffer) return;
    this.analyser.getFloatTimeDomainData(this.buffer);

    let sumOfSquares = 0;
    for (let index = 0; index < this.buffer.length; index += 1) {
      const sample = this.buffer[index];
      sumOfSquares += sample * sample;
    }
    const rms = Math.sqrt(sumOfSquares / this.buffer.length);

    const above = Math.max(0, rms - NOISE_FLOOR);
    // `HEADROOM` rather than a logarithm. A log scale is the right choice for a
    // meter with a dB axis and the wrong one here: it would make a whisper look
    // like ordinary speech, and the point of this value is that quiet and loud are
    // visibly different amounts of body movement.
    const target = Math.min(1, above / HEADROOM);

    this.level += (target - this.level) * SMOOTHING;
    for (const listener of this.listeners) listener(this.level);
    this.raf = window.requestAnimationFrame(this.loop);
  };
}
