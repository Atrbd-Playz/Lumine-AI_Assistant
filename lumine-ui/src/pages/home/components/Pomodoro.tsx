import { useEffect, useRef, useState } from "react";
import { Icon } from "./Icon";

/**
 * A pomodoro that actually runs one.
 *
 * ## The four things that make it a timer rather than a display
 *
 * - **Ends on a timestamp, not on a counter.** `endsAt` is read against the clock
 *   on every tick, so a backgrounded window, a sleeping laptop and a throttled
 *   `setInterval` all resume from the right second instead of from wherever the
 *   interval happened to stop. A countdown decremented per tick loses a minute a
 *   minute the moment the machine idles.
 * - **Auto-advances between phases**, and says which one it moved to. A pomodoro
 *   that stops at the end of a work block and waits to be restarted is a stopwatch
 *   with presets — the phase change *is* the feature.
 * - **Counts the rounds.** A session with no round count has no long break, and a
 *   long break that never comes is a short break with worse branding.
 * - **Pauses without losing the remaining time**, because `endsAt` is shifted by
 *   the remainder rather than recreated.
 *
 * The ring is an SVG stroke rather than a conic-gradient div: a gradient needs
 * `@property` to animate and a stroke only needs `stroke-dashoffset`, and the arc
 * is the one piece of information here that type cannot carry — you look at a
 * timer sideways and know how much is left without reading it.
 */
type Phase = "work" | "short" | "long";

const LENGTHS: Record<Phase, number> = { work: 25, short: 5, long: 15 };
const PHASE_LABEL: Record<Phase, string> = { work: "Focus", short: "Short break", long: "Long break" };
/** Rounds of work before the long break. Four is the number the method is built on. */
const LONG_EVERY = 4;

const RING_RADIUS = 66;

export function Pomodoro() {
  const [phase, setPhase] = useState<Phase>("work");
  const [minutes, setMinutes] = useState<number>(LENGTHS.work);
  const [endsAt, setEndsAt] = useState<number | null>(null);
  const [running, setRunning] = useState(false);
  const [round, setRound] = useState(0);
  // Read through a ref in the ticker: the interval is created once per run and a
  // closure over `round` would report the value the interval was born with.
  const roundRef = useRef(0);
  roundRef.current = round;

  const totalMs = Math.max(1, minutes) * 60_000;
  const now = useTick(running && endsAt !== null);
  const remaining = endsAt === null ? totalMs : Math.max(0, endsAt - now);
  const done = endsAt !== null && remaining <= 0;

  // The advance itself. It lives in an effect rather than inside the click
  // handler because the only place "the time ran out" is true is the tick.
  useEffect(() => {
    if (!done || !running) return;
    const next: Phase =
      phase === "work"
        ? roundRef.current + 1 >= LONG_EVERY
          ? "long"
          : "short"
        : "work";
    if (phase === "work") setRound((value) => (value + 1) % LONG_EVERY);
    setPhase(next);
    setMinutes(LENGTHS[next]);
    setRunning(false);
    setEndsAt(null);
    notifyDone(next);
  }, [done, running, phase]);

  const circumference = 2 * Math.PI * RING_RADIUS;
  const progress = 1 - remaining / totalMs;

  const seconds = Math.ceil(remaining / 1000);
  const mm = Math.floor(seconds / 60);
  const ss = seconds % 60;
  const readout = `${String(mm).padStart(2, "0")}:${String(ss).padStart(2, "0")}`;

  const start = () => {
    setEndsAt(Date.now() + (endsAt === null || done ? totalMs : remaining));
    setRunning(true);
  };
  const pause = () => setRunning(false);
  const reset = () => {
    setRunning(false);
    setEndsAt(null);
  };
  const choosePhase = (next: Phase) => {
    setPhase(next);
    setMinutes(LENGTHS[next]);
    reset();
  };

  return (
    <section className={`pomodoro${done ? " is-done" : ""}`} aria-label="Pomodoro timer">
      <header className="pomodoro-head flex items-baseline justify-between gap-4 w-full">
        <span className="pomodoro-phase text-foreground text-[14px] font-bold tracking-[-0.02em]">{PHASE_LABEL[phase]}</span>
        <span className="pomodoro-round">
          {/* `round` counts rounds *completed*, so it is one behind the round in
              progress — displayed raw, the second pass read "1 of 4", and
              `round` only reaches 3, so "4 of 4" was a screen the counter could
              never produce. The long break is chosen by the same `+ 1` in the
              effect above, so the two have always agreed about which round this
              is; only the label disagreed. */}
          {`${round + 1} of ${LONG_EVERY}`}
        </span>
      </header>

      <div className="pomodoro-dial">
        <svg viewBox="0 0 160 160" aria-hidden="true">
          <circle className="pomodoro-track" cx="80" cy="80" r={RING_RADIUS} />
          <circle
            className="pomodoro-fill"
            cx="80"
            cy="80"
            r={RING_RADIUS}
            strokeDasharray={circumference}
            strokeDashoffset={circumference * (1 - progress)}
          />
        </svg>
        <span className="pomodoro-readout">{readout}</span>
      </div>

      <div className="pomodoro-phases" role="group" aria-label="Phase">
        {(["work", "short", "long"] as const).map((value) => (
          <button
            key={value}
            type="button"
            className={value === phase ? "is-on" : ""}
            aria-pressed={value === phase}
            onClick={() => choosePhase(value)}
          >
            {PHASE_LABEL[value]}
          </button>
        ))}
      </div>

      <div className="pomodoro-actions flex gap-2 w-full max-w-80">
        <button type="button" className="widget-button primary" onClick={() => (running ? pause() : start())}>
          <Icon name={running ? "stop" : "timer"} size={15} />
          {running ? "Pause" : done ? "Again" : endsAt === null ? "Start" : "Resume"}
        </button>
        <button type="button" className="widget-button" onClick={reset} disabled={endsAt === null}>
          <Icon name="reset" size={15} />
          Reset
        </button>
      </div>

      <div className="pomodoro-lengths" role="group" aria-label={`${PHASE_LABEL[phase]} length`}>
        <span>Length</span>
        {[10, 15, 25, 30, 45, 50].map((value) => (
          <button
            key={value}
            type="button"
            className={value === minutes ? "is-on" : ""}
            onClick={() => {
              setMinutes(value);
              reset();
            }}
          >
            {value}m
          </button>
        ))}
      </div>
    </section>
  );
}

/**
 * The system notification the browser will actually show, and silence if it will
 * not. Asking for permission unprompted is how a prompt gets dismissed before it
 * is wanted, so permission is requested at the moment of the first completion —
 * which is the moment the answer is obviously yes.
 */
let permissionAsked = false;
function notifyDone(next: Phase): void {
  if (typeof Notification === "undefined") return;
  try {
    if (Notification.permission === "default" && !permissionAsked) {
      permissionAsked = true;
      void Notification.requestPermission();
    }
    if (Notification.permission !== "granted") return;
    new Notification(next === "work" ? "Break is over" : "Time for a break", {
      body: next === "work" ? "Back to it when you are ready." : "Stand up. Look at something far away.",
    });
  } catch {
    // A notification that cannot be shown is not worth interrupting the timer for.
  }
}

/** Ticks once a second, and only while something is counting. */
function useTick(active: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 500);
    return () => window.clearInterval(timer);
  }, [active]);
  return now;
}
