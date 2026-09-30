import type { LumineVoiceConnectionState } from "../../features/voice/useLumineVoice";
import type { ConversationAgentStatus } from "./conversation/types";
import type { LumineState } from "./types";

/**
 * The two mappings that decide what the user is told is happening.
 *
 * ## Why this is a file and not two ternaries
 *
 * It was two ternaries, and both were lossy in the same direction. Every status
 * that was not `speaking` or `listening` fell through to `thinking`, so pressing
 * the call button drew a thoughtful expression, a room that had just joined drew
 * the same thoughtful expression, and a failed call drew a relaxed idle face.
 * The one thing the user most needs to know during setup and failure is the one
 * thing the screen would not say.
 *
 * A ternary chain cannot express "every status has an answer" — it can only
 * express "there is a fallback, and the fallback is what unknown states get".
 * A `Record` over the source union says the opposite: exhaustiveness is checked
 * by the compiler, so adding a status to `LumineVoiceStatus` breaks the build
 * here instead of silently rendering as `thinking` in a release.
 *
 * ## Why the two maps are separate
 *
 * They look redundant and they are not. The presence layer has seven states and
 * the transcript header has six, and collapsing them would force one of the two
 * to be wrong. The presence has to say `online` — a room joined, nobody spoken
 * yet — because that is a visibly different moment from `listening`, and the
 * avatar is the only surface that can afford to be that precise. The transcript
 * header is a single line of text, and its honest job during that window is to
 * say a call is coming up, not to narrate the handshake.
 */

/**
 * Voice status -> what the presence layer shows.
 *
 * Total over `LumineVoiceConnectionState`. If the voice layer grows a state, this
 * is the file that stops compiling, and that is the intended moment to decide
 * what a person's face should do about it.
 */
export const PRESENCE_STATE_BY_STATUS: Record<LumineVoiceConnectionState, LumineState> = {
  // No room. This is the resting face, and it is also what a finished call
  // returns to — including after a failure, which is its own state below.
  disconnected: "idle",
  idle: "idle",
  // Teardown. The conversation is over from the user's side even though the room
  // is still closing, and keeping the face on `connecting` for those few hundred
  // milliseconds would claim a call is being made after it was ended.
  disconnecting: "idle",
  ending: "idle",

  // The window this whole map exists for. Room opening, worker waking, agent
  // being dispatched — three real steps that used to draw a thinking face.
  connecting: "connecting",
  initializing: "connecting",
  reconnecting: "connecting",

  // Joined, agent present, nothing said yet. Distinct from `listening` because it
  // is the moment before the first word, and it deserves its own face.
  connected: "online",
  online: "online",
  // A joined room with no speech for a beat. Open, so the transcript can honestly
  // call it listening; not `online`, because nothing is being negotiated.
  waiting: "online",

  listening: "listening",
  thinking: "thinking",
  speaking: "speaking",
  error: "error",
};

/**
 * Presence state -> the transcript header's status.
 *
 * `online` lands on `listening` and not on a state of its own, and that is a
 * deliberate narrowing rather than an omission: the room is open and she is
 * waiting to be spoken to, which is exactly what `listening` says. The presence
 * can afford finer detail because it has a face to spend it on.
 */
export const CONVERSATION_STATUS_BY_PRESENCE: Record<LumineState, ConversationAgentStatus> = {
  idle: "idle",
  connecting: "connecting",
  online: "listening",
  listening: "listening",
  thinking: "thinking",
  speaking: "speaking",
  error: "error",
};
