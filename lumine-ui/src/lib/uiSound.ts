/**
 * Two synthesised cues, and nothing else.
 *
 * ## Why synthesised rather than a file
 *
 * A `.wav` in `public/` would be a third-party sound to license, a binary in a
 * review, and a request that can fail — all to produce two notes. The Web Audio
 * API renders an oscillator at play time, so the whole feature is the four
 * lines of envelope below and it cannot 404. It also means the sound is
 * *ours*: a two-note rising chime for a question and a single low tick for a
 * deadline, both quieter than anything the OS plays, because both arrive while
 * somebody is already listening to a voice conversation.
 *
 * ## Why every call is guarded
 *
 * These run on arrival paths — a permission request and its expiry — where a
 * thrown exception would break the thing being announced. WebView2 may have no
 * `AudioContext` at all, or may hold one suspended until a gesture has happened
 * (Chromium's autoplay policy). So: resume when suspended, swallow every
 * failure, and never return a rejected promise. A cue that does not play is a
 * missed notification; a cue that throws is a failed approval card.
 *
 * ## One context, created lazily
 *
 * An `AudioContext` is a real OS resource with its own thread, so it is made
 * once on the first cue and closed by nothing — a context that is created and
 * abandoned leaks a thread per call, and this one is only ever used while the
 * app is open. Creating it on first use rather than at import keeps a browser
 * (or a test) from opening audio hardware just by loading the bundle.
 */

let context: AudioContext | null = null;

/** The shared context, or `null` when Web Audio is unavailable. */
function audio(): AudioContext | null {
  if (context) {
    if (context.state === "suspended") {
      // Best effort: this resolves only once a gesture has been seen, which by
      // the time an approval arrives it has been. Failures are ignored — the
      // context stays suspended and the cue is silent, which is the safe end.
      void context.resume().catch(() => undefined);
    }
    return context;
  }

  const Ctor =
    window.AudioContext ??
    (window as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
  if (!Ctor) return null;

  try {
    context = new Ctor();
    if (context.state === "suspended") void context.resume().catch(() => undefined);
    return context;
  } catch {
    return null;
  }
}

/** One oscillator, one envelope, released after it has finished. */
function tone(
  when: number,
  frequency: number,
  seconds: number,
  gain: number,
  type: OscillatorType = "sine",
): void {
  const ctx = audio();
  if (!ctx) return;

  try {
    const start = ctx.currentTime + when;
    const oscillator = ctx.createOscillator();
    const envelope = ctx.createGain();
    oscillator.type = type;
    oscillator.frequency.setValueAtTime(frequency, start);

    // A short attack (so it does not click) and an exponential release (so it
    // falls away like a bell rather than being cut off). Exponential ramps
    // cannot target zero, hence the floor.
    envelope.gain.setValueAtTime(0.0001, start);
    envelope.gain.exponentialRampToValueAtTime(gain, start + 0.012);
    envelope.gain.exponentialRampToValueAtTime(0.0001, start + seconds);

    oscillator.connect(envelope).connect(ctx.destination);
    oscillator.start(start);
    oscillator.stop(start + seconds + 0.02);
    oscillator.onended = () => {
      oscillator.disconnect();
      envelope.disconnect();
    };
  } catch {
    // Never let a cue take the card that announced it.
  }
}

/**
 * A question has arrived: two rising notes, about 200 ms of sound.
 *
 * Rising rather than falling, because a fall is what this app's error toasts
 * use and the two must not be confusable. It plays only when there is **no**
 * system notification carrying the same arrival — the OS has its own sound for
 * that, and two chimes for one question is the audio equivalent of a duplicate
 * card.
 */
export function playApprovalChime(): void {
  tone(0, 783.99, 0.16, 0.055); // G5
  tone(0.1, 1046.5, 0.2, 0.05, "triangle"); // C6
}

/**
 * The deadline passed with no answer, and the worker has already refused.
 *
 * Low, single, and quiet: it marks an *absence* of a decision, so it must not
 * sound like an event of its own — a loud one would read as an error the user
 * caused, when the honest fact is that the clock ran out.
 */
export function playExpiryTick(): void {
  tone(0, 311.13, 0.09, 0.04, "triangle"); // Eb4
}
