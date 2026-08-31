/** Keeps a progress bar moving between server updates.
 *
 * Updates arrive a couple of times a second, which is plenty of information but
 * not enough to look continuous: the bar sits still, jumps, sits still again.
 *
 * The encoder reports how far into the source it is and how fast it is working,
 * and both change slowly. So between updates the position is extrapolated from
 * the last known speed, and each new update re-anchors it. The estimate is
 * capped so it can never run past what the next update could plausibly report,
 * and it never moves backwards - a bar that retreats looks broken even when the
 * number behind it is more accurate.
 */
import { useEffect, useRef, useState } from "react";

export interface ProgressSample {
  progress: number;      // 0..1 as last reported
  speed?: number;        // encoder speed, x realtime
  out_time?: number;     // seconds of source encoded
  duration?: number;     // total source seconds
  eta_seconds?: number;
}

/** How far ahead of the last update the estimate may run, in seconds. */
const MAX_EXTRAPOLATION = 3.0;

/** Smallest change worth a re-render - below this nothing moves on screen. */
const VISIBLE_STEP = 0.0004; // 0.04% of the bar

export function useSmoothProgress(sample: ProgressSample | null | undefined): number {
  const [display, setDisplay] = useState(sample?.progress ?? 0);
  const anchor = useRef({ at: 0, progress: 0, rate: 0 });
  const shown = useRef(display);

  // Re-anchor whenever a real update lands.
  useEffect(() => {
    if (!sample) return;
    const progress = clamp01(sample.progress);
    // Fraction of the file completed per second of wall clock.
    const duration = sample.duration ?? 0;
    const rate = duration > 0 && sample.speed && sample.speed > 0 ? sample.speed / duration : 0;

    anchor.current = { at: performance.now(), progress, rate };
    // Never step backwards on a re-anchor; wait for reality to catch up.
    if (progress >= shown.current || progress === 0) {
      shown.current = progress;
      setDisplay(progress);
    }
  }, [sample?.progress, sample?.speed, sample?.duration]);

  // Between updates, walk forward at the last known rate.
  useEffect(() => {
    if (!sample) return;
    if (anchor.current.rate <= 0) return;
    if (sample.progress >= 1) return;

    let frame = 0;
    const tick = () => {
      const { at, progress, rate } = anchor.current;
      const elapsed = Math.min((performance.now() - at) / 1000, MAX_EXTRAPOLATION);
      const estimate = clamp01(progress + rate * elapsed);
      // Re-render only when the bar would actually move.  On a slow encode the
      // per-frame change is far below a pixel, and 60 renders a second for an
      // invisible difference is wasted work.
      if (estimate > shown.current + VISIBLE_STEP) {
        shown.current = estimate;
        setDisplay(estimate);
      }
      frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [sample?.progress, sample?.speed, sample?.duration]);

  return display;
}

/** Countdown that ticks down every second instead of jumping on each update. */
export function useSmoothEta(etaSeconds: number | undefined): number {
  const [eta, setEta] = useState(etaSeconds ?? 0);
  const anchor = useRef({ at: Date.now(), value: etaSeconds ?? 0 });

  useEffect(() => {
    if (etaSeconds === undefined) return;
    anchor.current = { at: Date.now(), value: etaSeconds };
    setEta(etaSeconds);
  }, [etaSeconds]);

  useEffect(() => {
    if (!etaSeconds) return;
    const timer = window.setInterval(() => {
      const elapsed = (Date.now() - anchor.current.at) / 1000;
      setEta(Math.max(0, Math.round(anchor.current.value - elapsed)));
    }, 1000);
    return () => window.clearInterval(timer);
  }, [etaSeconds]);

  return eta;
}

function clamp01(value: number): number {
  if (!Number.isFinite(value)) return 0;
  return Math.min(1, Math.max(0, value));
}
