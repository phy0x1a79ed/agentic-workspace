/**
 * Pure derivation of a task's steering-chip state from the 3s DAG poll fields.
 *
 * The steering protocol is durable and server-authoritative: `attached` and the
 * consent bits change from BOTH sides (the user attaches / consents; the agent
 * signals readiness; the mutual-detach handshake clears everything at once), so
 * the UI only ever READS them — it never optimistically overlays them. This
 * helper collapses those raw bits into one compact chip descriptor + the
 * meaningful sub-flags, so the FocusPanel / TaskList / AttentionStrip all render
 * the same state the same way.
 *
 *   viewing        — default: not attached, no pending steering.
 *   wants steering — `steer_requested`: a detached, mid-run node asking for a
 *                    human (it keeps working; this is a flag, not a freeze).
 *   attached       — a human is steering (supervisor frozen). Sub-flags:
 *                      · agent ready (`steer_agent_ready`)
 *                      · you: done   (`steer_user_done`)
 *                    both set → the handshake is about to fire (detaching…).
 */
import type { TagTone } from './types';

/** Just the poll fields this helper reads (a structural subset of DagTask). */
export interface SteerFields {
  attached?: boolean;
  steer_user_done?: boolean;
  steer_agent_ready?: boolean;
  steer_requested?: boolean;
}

export type SteeringState = 'viewing' | 'wants_steering' | 'attached' | 'detaching';

export interface SteeringChip {
  /** The primary state (drives the headline chip label + tone). */
  state: SteeringState;
  /** Compact human label for the primary chip. */
  label: string;
  tone: TagTone;
  /** Raw sub-flags, meaningful while attached (rendered as small markers). */
  attached: boolean;
  agentReady: boolean;
  userDone: boolean;
  wantsSteering: boolean;
}

export function deriveSteering(t: SteerFields): SteeringChip {
  const attached = !!t.attached;
  const agentReady = !!t.steer_agent_ready;
  const userDone = !!t.steer_user_done;
  const wantsSteering = !!t.steer_requested;

  let state: SteeringState;
  let label: string;
  let tone: TagTone;

  if (attached) {
    if (agentReady && userDone) {
      // Both bits agree — the handshake fires on the next boundary.
      state = 'detaching';
      label = 'detaching…';
      tone = 'ok';
    } else {
      state = 'attached';
      label = 'attached';
      tone = 'atomizer';
    }
  } else if (wantsSteering) {
    state = 'wants_steering';
    label = 'wants steering';
    tone = 'warn';
  } else {
    state = 'viewing';
    label = 'viewing';
    tone = 'neutral';
  }

  return { state, label, tone, attached, agentReady, userDone, wantsSteering };
}
