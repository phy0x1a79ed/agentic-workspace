// One row in the dev telemetry timeline.
//
// `t` is a `performance.now()` millisecond stamp taken at emit (outbound /
// inbound-frame events) or at arrival (backend `metric` frames) — monotonic
// within a page load, so deltas between rows are stable. The panel derives both
// the absolute time and the Δ-from-previous from `t`.
//
// `dir` colors the row by where the event came from:
//   - 'out'     : something the browser sent up (audio chunk, control frame)
//   - 'in'      : a normal frame the backend sent down (partial, composer, …)
//   - 'backend' : a dev `metric` frame carrying a server-measured duration
//
// `dur` is the measured "took" of a timed operation (backend metrics, mostly).
// `detail` is a short precomputed string (snippet) — never raw payloads, so
// render stays cheap over a long session. For `audio_chunk` events `detail` is
// computed by the accumulator (see ./log), not the emitter.
// `count` / `bytes` are the coalesced chunk count and total bytes of an
// `audio_chunk` burst row; `bytes` is set on each single chunk by the emitter so
// the accumulator can sum it.
export type TelemetryEvent = {
  t: number;
  dir: 'out' | 'in' | 'backend';
  event: string;
  dur?: number;
  detail?: string;
  count?: number;
  bytes?: number;
};
