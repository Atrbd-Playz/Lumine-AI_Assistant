import * as React from "react"
import { Toast as ToastPrimitive } from "@base-ui/react/toast"
import { cn } from "cn"

import { Button } from "@/components/ui/button"
import { XIcon, CheckCircleIcon, InfoIcon, WarningIcon, XCircleIcon, SpinnerIcon } from "@phosphor-icons/react"

const toast = ToastPrimitive.createToastManager()

function ToastProvider({ ...props }: ToastPrimitive.Provider.Props) {
  return <ToastPrimitive.Provider {...props} />
}

function ToastPortal({ ...props }: ToastPrimitive.Portal.Props) {
  return <ToastPrimitive.Portal data-slot="toast-portal" {...props} />
}

/**
 * Top-centre, and deliberately not a bottom corner.
 *
 * The voice dock spans the full width of the command space, so a bottom-anchored
 * toast sits on top of the one control that starts a session and swallows its
 * clicks. The top centre is the one band of the window that carries nothing but
 * the date and two small toggles, and it stays clear of the settings sheet
 * because this stacks above it.
 *
 * The stack is a plain flow column rather than base-ui's overlapping peek. With
 * one region to avoid, overlap buys nothing and costs a set of transform
 * variables that have to be inverted to grow downward instead of upward.
 */
function ToastViewport({ className, ...props }: ToastPrimitive.Viewport.Props) {
  return (
    <ToastPrimitive.Viewport
      data-slot="toast-viewport"
      className={cn(
        "pointer-events-none fixed top-4 left-1/2 z-[100] flex w-[min(26rem,calc(100vw-2rem))] -translate-x-1/2 flex-col gap-2 outline-none",
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
      className={cn(
        "group/toast pointer-events-auto relative flex w-full items-stretch overflow-hidden rounded-[var(--radius-md)] border border-[var(--color-border)] bg-[color-mix(in_srgb,var(--color-surface)_92%,transparent)] text-[var(--color-text)] shadow-[0_18px_44px_-12px_rgba(0,0,0,0.55)] outline-none select-none backdrop-blur-xl backdrop-saturate-150",
        // A tone wash plus a leading rule, so the kind of message is readable
        // from the edge of the screen without reading the words.
        "before:absolute before:inset-y-0 before:left-0 before:w-[3px] before:bg-[var(--toast-tone,var(--color-accent))]",
        // The tones are tokens rather than hexes: #5fbf8f reached 2.2:1 on the
        // light toast surface, below the 3:1 a graphic is held to, and none of
        // the three switched when the theme did. `--color-ok` / `--color-danger`
        // / `--color-warn` are declared per theme for exactly this.
        "data-[tone=success]:[--toast-tone:var(--color-ok)] data-[tone=error]:[--toast-tone:var(--color-danger)] data-[tone=warning]:[--toast-tone:var(--color-warn)] data-[tone=info]:[--toast-tone:var(--color-accent)] data-[tone=loading]:[--toast-tone:var(--color-text-faint)]",
        "focus-visible:ring-2 focus-visible:ring-[var(--color-accent)] focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--color-canvas)]",
        // A flow column, so the only transform is the swipe offset. Enter and
        // exit slide down from the top edge, matching where the stack begins.
        "[transform:translateX(var(--toast-swipe-movement-x))] [transition:transform_380ms_cubic-bezier(0.22,1,0.36,1),opacity_220ms_ease-out,margin_220ms_ease-out]",
        "data-starting-style:opacity-0 data-starting-style:-translate-y-2",
        "data-ending-style:opacity-0 data-ending-style:-translate-y-2",
        "data-limited:hidden",
        className
      )}
      {...props}
    />
  )
}

function ToastContent({ className, ...props }: ToastPrimitive.Content.Props) {
  return (
    <ToastPrimitive.Content
      data-slot="toast-content"
      className={cn(
        "flex h-full w-full items-start gap-3 overflow-hidden py-3 pl-4 pr-2 transition-opacity duration-200 ease-[cubic-bezier(0.22,1,0.36,1)] data-behind:opacity-0 data-expanded:opacity-100",
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
        "font-[var(--font-ui)] text-[0.8125rem] leading-snug font-semibold tracking-[-0.01em]",
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
      className={cn(
        "font-[var(--font-ui)] text-[0.75rem] leading-relaxed text-[var(--color-text-soft)] wrap-break-word",
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

function ToastIcon({ type }: { type: string | undefined }) {
  const icon = type ? TONE_ICON[type] : null
  if (!icon) return null

  return (
    <span
      data-slot="toast-icon"
      className="mt-px shrink-0 text-[var(--toast-tone,var(--color-accent))] [&_svg]:pointer-events-none [&_svg:not([class*='size-'])]:size-[1.05rem]"
    >
      {icon}
    </span>
  )
}

function ToastList() {
  const { toasts } = ToastPrimitive.useToastManager()

  return toasts.map((toastItem) => (
    <Toast key={toastItem.id} toast={toastItem} data-tone={toastItem.type}>
      <ToastContent>
        <ToastIcon type={toastItem.type} />
        <div className="flex min-w-0 flex-1 flex-col gap-0.5">
          <ToastTitle />
          <ToastDescription />
        </div>
        <ToastAction />
        <ToastClose />
      </ToastContent>
    </Toast>
  ))
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
