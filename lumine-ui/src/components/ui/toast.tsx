import * as React from "react"
import { Toast as ToastPrimitive } from "@base-ui/react/toast"
import { cn } from "cn"

import { Button } from "@/components/ui/button"
import { XIcon, CheckCircleIcon, InfoIcon, WarningIcon, XCircleIcon, SpinnerIcon } from "@phosphor-icons/react"

const toast = ToastPrimitive.createToastManager()

/**
 * How many notifications may be on screen at once.
 *
 * A hard cap, and the reason it is a cap rather than "however many arrive": a
 * notification queue with no ceiling is a queue that eventually becomes a wall,
 * and a wall in front of the app is worse than the notifications it was built
 * from. Three is enough to read a burst in — the failure circuit, a tool
 * result, the answer that followed — and anything beyond that is summarised by
 * the "+N more" line rather than drawn.
 *
 * base-ui marks the *oldest* survivors `data-limited` when the cap is exceeded,
 * so the ones that drop out are the ones that were closest to expiring anyway.
 */
const MAX_VISIBLE = 3

function ToastProvider({ ...props }: ToastPrimitive.Provider.Props) {
  return <ToastPrimitive.Provider limit={MAX_VISIBLE} {...props} />
}

function ToastPortal({ ...props }: ToastPrimitive.Portal.Props) {
  return <ToastPrimitive.Portal data-slot="toast-portal" {...props} />
}

/**
 * Bottom-left of the command space, and deliberately not the top centre.
 *
 * ## Why this corner
 *
 * The top centre was the previous home, and it was the wrong one: the stage's
 * topbar sits directly beneath it, so every notification sat on the date, the
 * "Command space" heading and the search control — the one band of the window
 * that is almost entirely interactive. The other three corners are spoken for:
 *
 * - top-right is the topbar's own actions, or the transcript's header when the
 *   panel is open;
 * - bottom-centre is the call bar, the control that starts a session;
 * - bottom-right is the transcript's composer, which is where somebody is
 *   halfway through typing.
 *
 * The lower-left is the one region of this window with no control in it. Both
 * offsets are stated in the layout's own terms — the rail's width and the call
 * strip's height — so the stack cannot drift onto the rail when it folds or onto
 * the call bar when that row grows. Behind it is the left end of the widget band:
 * cards nobody clicks.
 *
 * On a phone the rail becomes a full-width thumb bar along the bottom edge, so
 * the stack moves to the top instead of sitting on top of it. The breakpoint is
 * the same `780px` the layout already uses, so the two cannot disagree.
 *
 * ## A column that grows up from the bottom
 *
 * `flex-col` with the newest last, which puts the newest card nearest the
 * corner — the position the eye returns to. This is a plain flow column rather
 * than base-ui's overlapping peek, and `ToastContent` deliberately does **not**
 * honour `data-behind`: see the note on that component for what the peek costs.
 */
function ToastViewport({ className, ...props }: ToastPrimitive.Viewport.Props) {
  return (
    <ToastPrimitive.Viewport
      data-slot="toast-viewport"
      className={cn(
        "pointer-events-none fixed bottom-[calc(var(--call-h)+12px)] left-[calc(var(--sidebar-width)+1rem)] z-[100] flex w-[min(21rem,calc(100vw-2rem))] flex-col gap-2 outline-none",
        "max-[780px]:inset-x-4 max-[780px]:bottom-auto max-[780px]:top-4 max-[780px]:w-auto",
        className
      )}
      {...props}
    />
  )
}

function Toast({ className, ...props }: ToastPrimitive.Root.Props) {
  return (
    <ToastPrimitive.Root
      data-slot="toast"
      // Left, because this stack is anchored on the left and a card you can
      // throw towards the edge is a card that leaves the way it arrived.
      swipeDirection="left"
      className={cn(
        "group/toast pointer-events-auto relative flex w-full items-stretch overflow-hidden rounded-[var(--radius-md)] border text-[var(--color-text)] outline-none select-none",
        // A tone wash on the surface and a tone-tinted border, not a flat grey
        // card with a coloured stripe. The kind of message has to be readable
        // from the edge of the screen without reading the words, and a wash does
        // that without turning the panel into a set of coloured rectangles.
        "border-[color-mix(in_srgb,var(--toast-tone)_24%,var(--color-border))] bg-[color-mix(in_srgb,var(--toast-tone)_7%,var(--color-surface))] shadow-[var(--elevation-3)]",
        "before:absolute before:inset-y-1.5 before:left-0 before:w-[2.5px] before:rounded-full before:bg-[var(--toast-tone)]",
        // The tones are tokens rather than hexes: `#5fbf8f` reached 2.2:1 on the
        // light toast surface, below the 3:1 a graphic is held to, and none of
        // the three switched when the theme did. `--color-ok` / `--color-danger`
        // / `--color-warn` are declared per theme for exactly this.
        "data-[tone=success]:[--toast-tone:var(--color-ok)] data-[tone=error]:[--toast-tone:var(--color-danger)] data-[tone=warning]:[--toast-tone:var(--color-warn)] data-[tone=info]:[--toast-tone:var(--color-accent)] data-[tone=loading]:[--toast-tone:var(--color-text-faint)]",
        "focus-visible:ring-2 focus-visible:ring-[var(--color-accent)] focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--color-canvas)]",
        // Swipe offset on the inline axis only, so the card keeps its own
        // stacking and the enter/exit animations can still translate vertically.
        "[transform:translateX(var(--toast-swipe-movement-x))] [transition:transform_380ms_cubic-bezier(0.22,1,0.36,1),opacity_220ms_ease-out,margin_220ms_ease-out]",
        "data-starting-style:opacity-0 data-starting-style:translate-y-2",
        "data-ending-style:opacity-0 data-ending-style:-translate-y-1 data-ending-style:scale-[0.98]",
        "data-limited:hidden",
        className
      )}
      {...props}
    />
  )
}

/**
 * The card's body, and one deliberate omission.
 *
 * base-ui marks every toast behind the frontmost one `data-behind` and every
 * toast `data-expanded` when the viewport has focus or the pointer. Its own
 * peek is built out of those two: fold the ones behind into a 2px strip, keep the
 * frontmost readable, and restore them on hover.
 *
 * This stylesheet draws neither, and the reason is that `data-behind` used to be
 * wired to `opacity: 0` here — which with a flow-column viewport did not produce a
 * peek at all. It produced a stack of **blank coloured cards**: the frontmost
 * toast readable and every one behind it drawn at full size with its contents
 * invisible. A second card behind the first is not a thing anyone needs hidden;
 * it is a second thing that happened.
 *
 * So the viewport's column is the whole behaviour: every card is fully readable,
 * every card is fully drawn, and the cap on how many there are is the provider's
 * `limit`. The attributes stay available for anyone who later wants a real peek,
 * because they are still on the element.
 */
function ToastContent({ className, ...props }: ToastPrimitive.Content.Props) {
  return (
    <ToastPrimitive.Content
      data-slot="toast-content"
      className={cn(
        "flex h-full w-full items-start gap-2.5 overflow-hidden py-2.5 pr-1.5 pl-3",
        className
      )}
      {...props}
    />
  )
}

function ToastTitle({ className, ...props }: ToastPrimitive.Title.Props) {
  return (
    <ToastPrimitive.Title
      data-slot="toast-title"
      className={cn(
        "font-[var(--font-ui)] text-[0.78125rem] leading-[1.35] font-semibold tracking-[-0.01em]",
        className
      )}
      {...props}
    />
  )
}

function ToastDescription({ className, ...props }: ToastPrimitive.Description.Props) {
  return (
    <ToastPrimitive.Description
      data-slot="toast-description"
      // Clamped at three lines as a floor on card height. Nothing in the app
      // writes a paragraph here — tool payloads are capped by `toolToast` and
      // failures are one sentence from `categorizeError` — so this only ever
      // catches a provider string that quoted its own body back at us.
      className={cn(
        "line-clamp-3 font-[var(--font-ui)] text-[0.71875rem] leading-[1.45] text-[var(--color-text-soft)] wrap-break-word",
        className
      )}
      {...props}
    />
  )
}

function ToastAction({
  className,
  render = <Button variant="outline" size="sm" />,
  ...props
}: ToastPrimitive.Action.Props) {
  return (
    <ToastPrimitive.Action
      data-slot="toast-action"
      render={render}
      className={cn("shrink-0 self-center", className)}
      {...props}
    />
  )
}

function ToastClose({
  className,
  children,
  render = <Button variant="ghost" size="icon-sm" />,
  ...props
}: ToastPrimitive.Close.Props) {
  return (
    <ToastPrimitive.Close
      data-slot="toast-close"
      aria-label="Dismiss notification"
      render={render}
      className={cn(
        "relative shrink-0 self-center text-[var(--color-text-faint)] transition-colors after:absolute after:-inset-2 after:content-[''] hover:text-[var(--color-text)]",
        className
      )}
      {...props}
    >
      {children ?? <XIcon aria-hidden="true" />}
    </ToastPrimitive.Close>
  )
}

const TONE_ICON: Record<string, React.ReactNode> = {
  success: <CheckCircleIcon aria-hidden="true" />,
  info: <InfoIcon aria-hidden="true" />,
  warning: <WarningIcon aria-hidden="true" />,
  error: <XCircleIcon aria-hidden="true" />,
  loading: <SpinnerIcon className="animate-spin" aria-hidden="true" />,
}

/**
 * The tone mark, in a tinted chip.
 *
 * A bare glyph next to a title reads as decoration; the same glyph on a wash of
 * its own colour reads as the badge it is, and it gives the card a left-hand
 * anchor so the title and the body do not both start at the same edge.
 */
function ToastIcon({ type }: { type: string | undefined }) {
  const icon = type ? TONE_ICON[type] : null
  if (!icon) return null

  return (
    <span
      data-slot="toast-icon"
      className="grid size-[22px] shrink-0 place-items-center rounded-[7px] bg-[color-mix(in_srgb,var(--toast-tone)_16%,var(--color-surface))] text-[var(--toast-tone)] [&_svg]:pointer-events-none [&_svg:not([class*='size-'])]:size-[0.8125rem]"
    >
      {icon}
    </span>
  )
}

function ToastList() {
  const { toasts } = ToastPrimitive.useToastManager()

  // Beyond the cap. base-ui keeps them in state and keeps their timers running,
  // so the honest statement is a count rather than a lie about them having gone.
  const queued = toasts.filter((entry) => entry.limited).length

  return (
    <>
      {queued > 0 && (
        <p className="toast-overflow pointer-events-none mb-0.5 self-center rounded-full px-2 py-[3px] text-center font-[var(--font-ui)] text-[10.5px] font-medium tracking-[0.01em] text-[var(--color-text-faint)]">
          +{queued} more queued
        </p>
      )}
      {toasts.map((toastItem) => (
        <Toast key={toastItem.id} toast={toastItem} data-tone={toastItem.type}>
          <ToastContent>
            <ToastIcon type={toastItem.type} />
            <div className="flex min-w-0 flex-1 flex-col">
              <ToastTitle />
              <ToastDescription />
            </div>
            <ToastAction />
            <ToastClose />
          </ToastContent>
        </Toast>
      ))}
    </>
  )
}

function Toaster({
  children,
  toastManager = toast,
  ...props
}: ToastPrimitive.Provider.Props) {
  return (
    <ToastProvider toastManager={toastManager} {...props}>
      {children}
      <ToastPortal>
        <ToastViewport>
          <ToastList />
        </ToastViewport>
      </ToastPortal>
    </ToastProvider>
  )
}

const createToastManager = ToastPrimitive.createToastManager
const useToastManager = ToastPrimitive.useToastManager

export {
  MAX_VISIBLE as TOAST_VISIBLE_LIMIT,
  Toaster,
  Toast,
  ToastAction,
  ToastClose,
  ToastContent,
  ToastDescription,
  ToastPortal,
  ToastProvider,
  ToastTitle,
  ToastViewport,
  createToastManager,
  toast,
  useToastManager,
}