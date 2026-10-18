import { useEffect } from "react";
import {
  isPermissionGranted,
  onAction,
  registerActionTypes,
  requestPermission,
  sendNotification,
  type ActionPerformed,
} from "@tauri-apps/plugin-notification";
import type { VoicePermissionRequest } from "./voice-manager";
import { playApprovalChime, playExpiryTick } from "../../lib/uiSound";

/**
 * The approval question as an **operating-system notification**, with working
 * Allow / Not now buttons.
 *
 * ## Why this exists
 *
 * The question is spoken and the card is drawn, and both of them land inside a
 * window the user may not be looking at. An approval that expires in 60 seconds
 * is a question asked of somebody who is reading something else — so the
 * question goes where their eyes already are, and the answer comes back from
 * there without the window being raised at all.
 *
 * ## How the answer gets back
 *
 * The plugin registers an *action type* (`lumine-approval`) whose two actions
 * are the same two words the card offers. Pressing one emits the plugin's
 * `action` event carrying the action id **and the notification's `extra`**, and
 * `extra` is where the worker's request id rides. So the handler resolves
 * `requestId` + `approve|deny` and hands it to the exact same `answerPermission`
 * the card calls — the worker receives an ordinary `permission_response` and
 * cannot tell which surface it came from, which is the point: there is one
 * answer channel, reached two ways.
 *
 * Nothing here decides anything. It renders the words the worker asked for and
 * returns the id it was given.
 *
 * ## Three rules that are all "the card still wins"
 *
 * 1. **A press on a question that is gone is dropped.** Ids are single-use
 *    tokens the worker minted for *this* session's pending set, so an answer
 *    arriving after expiry is refused there anyway; skipping it here means the
 *    UI never claims an answer it did not deliver.
 * 2. **Clicking the toast body (`actionId: "tap"`) is not a decision.** It
 *    raises the window and changes nothing — the way a card click outside the
 *    buttons does not answer the question either.
 * 3. **When a toast could not be sent, the in-app chime plays instead.** The OS
 *    has its own sound for a toast, and two cues for one question are the audio
 *    version of two cards. So the arrival sound belongs to whichever surface
 *    actually arrived.
 *
 * ## What this cannot do on Windows
 *
 * `removeActive` is Linux-only, so an answered toast stays on screen with
 * buttons that now go nowhere (rule 1 makes pressing them harmless). A click
 * reported after the toast has moved to the Action Center is not reported at
 * all. Both are platform facts, not gaps in this module: the in-app card is the
 * surface that is always correct, and the toast is an *additional* way to answer
 * rather than a replacement for it.
 */

export type ApprovalDecision = "approve" | "deny";

const ACTION_TYPE = "lumine-approval";
const APPROVE_ID = "approve";
const DENY_ID = "deny";

/** Module-level because both are *session* facts, not component facts. */
const seen = new Set<string>();
const clocks = new Map<string, { expiresAt: number; timer: number }>();

/** The current answer channel, so a listener registered once always reaches the live one. */
let answering: ((id: string, decision: ApprovalDecision) => Promise<boolean>) | null = null;

/**
 * Registration, exactly once for the life of the app.
 *
 * The listener has to exist **before** the first toast goes out: on desktop the
 * plugin shows a notification without its buttons unless one is listening, and
 * a toast without Allow / Not now is a picture of a question that cannot be
 * answered — strictly worse than no toast, because it consumes the one thing
 * the card needed (the user's attention) and returns nothing for it.
 */
let ready: Promise<boolean> | null = null;
function ensureReady(): Promise<boolean> {
  if (!ready) {
    ready = (async () => {
      await registerActionTypes([
        {
          id: ACTION_TYPE,
          actions: [
            { id: APPROVE_ID, title: "Allow" },
            { id: DENY_ID, title: "Not now" },
          ],
        },
      ]);
      await onAction(handleAction);
      return true;
    })().catch(() => false);
  }
  return ready;
}

/** One press on a toast, back into the answer channel the card already uses. */
function handleAction(event: ActionPerformed): void {
  const decision: ApprovalDecision | null =
    event.actionId === APPROVE_ID ? "approve" : event.actionId === DENY_ID ? "deny" : null;
  // `tap` and anything unrecognised are a request to *look*, not to decide.
  if (!decision || !answering) return;

  const id = event.notification?.extra?.requestId;
  if (typeof id !== "string" || id.length === 0) return;

  void answering(id, decision).catch(() => undefined);
}

/** The OS permission, asked for once and only if it is not already there. */
async function hasPermission(): Promise<boolean> {
  try {
    if (await isPermissionGranted()) return true;
    return (await requestPermission()) === "granted";
  } catch {
    return false;
  }
}

/** `send_whatsapp` -> `send whatsapp`. A toast title is not an identifier. */
function headline(request: VoicePermissionRequest): string {
  return `Lumine · ${request.tool.replace(/_/g, " ")}`;
}

/**
 * The toast body: the exact question, then the exact target, then the deadline.
 *
 * The question only — never the summary beside it. The worker builds
 * `question = summary + suffix`, so printing both is the same sentence twice on
 * a surface whose whole job is to be read in one glance.
 */
function body(request: VoicePermissionRequest, at: number): string {
  const lines = [request.question];
  if (request.target) lines.push(`On: ${request.target}`);
  const seconds = Math.max(0, Math.ceil(request.expiresAt - at / 1000));
  if (seconds > 0) lines.push(`${seconds}s to answer, or say yes / no out loud.`);
  return lines.join("\n");
}

/**
 * Show one request as a notification. Resolves to whether it was actually
 * delivered; `false` means "fall back to the card and the chime", and is never
 * a throw — a notification failing must not be able to fail the approval flow
 * it was only ever decorating.
 */
async function notify(request: VoicePermissionRequest): Promise<boolean> {
  if (!(await ensureReady())) return false;
  if (!(await hasPermission())) return false;

  try {
    sendNotification({
      title: headline(request),
      body: body(request, Date.now()),
      actionTypeId: ACTION_TYPE,
      // The correlation token. It is echoed back on `ActionPerformed`, which is
      // the only way two toasts on screen at once can be told apart.
      extra: { requestId: request.id },
    });
    return true;
  } catch {
    return false;
  }
}

/**
 * Owns every approval's arrival and deadline, for the life of the page.
 *
 * Mounted once by `Home.tsx`, beside the state it observes. It deliberately
 * does not render anything: the card is a separate component with its own
 * reason to exist, and this is the part that has to keep running *after* the
 * card is swept — which is why the deadline is tracked here in module-level
 * timers rather than in a component whose unmount would cancel them.
 */
export function useApprovalNotifications(
  requests: readonly VoicePermissionRequest[],
  answer: (id: string, decision: ApprovalDecision) => Promise<boolean>,
): void {
  // The listener is registered once, so it holds a pointer rather than a
  // closure: this effect only keeps that pointer aimed at the current callback.
  useEffect(() => {
    answering = answer;
    return () => {
      if (answering === answer) answering = null;
    };
  }, [answer]);

  useEffect(() => {
    const live = new Set(requests.map((request) => request.id));

    // Requests that have left the list. Two ways out, and only one of them is a
    // deadline: answered early clears its clock with no sound, while one whose
    // deadline passed gets the tick it never got — because the sweep runs on a
    // one-second interval and would otherwise remove the card between two
    // renders with nothing to show for it.
    for (const [id, clock] of [...clocks]) {
      if (live.has(id)) continue;
      window.clearTimeout(clock.timer);
      clocks.delete(id);
      seen.delete(id);
      if (Date.now() >= clock.expiresAt * 1000) playExpiryTick();
    }

    for (const request of requests) {
      if (seen.has(request.id)) continue;
      seen.add(request.id);

      // Fire and forget: the card is already on screen by the time this runs, so
      // a slow or refused notification degrades to *the same state as before*
      // plus a chime, never to a missing approval.
      void notify(request).then((delivered) => {
        if (!delivered) playApprovalChime();
      });

      const ms = Math.max(0, request.expiresAt * 1000 - Date.now());
      const timer = window.setTimeout(() => {
        clocks.delete(request.id);
        seen.delete(request.id);
        playExpiryTick();
      }, ms);
      clocks.set(request.id, { expiresAt: request.expiresAt, timer });
    }
  }, [requests]);

  // Timers outlive every render but not the page, and nothing else unmounts
  // this hook — so the only teardown worth registering is the app's.
  useEffect(
    () => () => {
      for (const clock of clocks.values()) window.clearTimeout(clock.timer);
      clocks.clear();
    },
    [],
  );
}
