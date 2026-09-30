import { Button as ButtonPrimitive } from "@base-ui/react/button"
import { cva, type VariantProps } from "class-variance-authority"
import { cn } from "cn"

/**
 * The shared button primitive.
 *
 * Colours come from Lumine's own tokens rather than the shadcn defaults, so a
 * primitive dropped into the app inherits the active palette — including a
 * custom one — instead of arriving as a grey rectangle from another theme.
 */
const buttonVariants = cva(
  "group/button inline-flex shrink-0 items-center justify-center rounded-[var(--radius-sm)] border border-transparent bg-clip-padding font-[var(--font-ui)] text-xs font-medium whitespace-nowrap transition-[background-color,border-color,color,transform,box-shadow] duration-150 outline-none select-none focus-visible:border-[var(--color-accent)] focus-visible:ring-2 focus-visible:ring-[var(--color-accent)]/35 active:not-aria-[haspopup]:translate-y-px disabled:pointer-events-none disabled:opacity-45 [&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*='size-'])]:size-4",
  {
    variants: {
      variant: {
        default:
          "bg-[var(--color-accent)] text-[var(--color-stage-text)] shadow-[0_6px_18px_-8px_var(--color-accent)] hover:brightness-110",
        outline:
          "border-[var(--color-border)] bg-transparent text-[var(--color-text-soft)] hover:border-[var(--color-accent)]/55 hover:bg-[var(--color-surface-muted)] hover:text-[var(--color-text)]",
        secondary:
          "border-[var(--color-border)] bg-[var(--color-surface-muted)] text-[var(--color-text)] hover:border-[var(--color-accent)]/45 hover:bg-[color-mix(in_srgb,var(--color-accent)_14%,var(--color-surface-muted))]",
        ghost:
          "text-[var(--color-text-soft)] hover:bg-[var(--color-surface-muted)] hover:text-[var(--color-text)]",
        // The tint, border and ring are the token rather than a hex, because
        // `#e2685a` matched neither theme's danger colour: light declares `#b03a2e`
        // and dark `#e5645a`, so the button was a red belonging to a third palette
        // in both of them. The *text* is measured against that tint and does not
        // simply inherit it — on the light 15% fill the token reaches 4.53:1, while
        // the dark token manages only 4.0:1 against a dark fill of itself, so dark
        // takes a lifted tint of the same hue. Hover needs its own per theme for the
        // mirror-image reason: the fill darkens, which helps a light-on-dark glyph
        // and costs a dark-on-light one a full point.
        destructive:
          "border-destructive/45 bg-destructive/15 text-destructive hover:border-destructive/70 hover:bg-destructive/25 hover:text-[#8f2a1e] focus-visible:border-destructive focus-visible:ring-destructive/35 dark:text-[#ff8f84] dark:hover:text-[#ffb0a6]",
        link: "text-[var(--color-accent)] underline-offset-4 hover:underline",
      },
      size: {
        default: "h-8 gap-1.5 px-3",
        xs: "h-6 gap-1 px-2 text-[0.6875rem] [&_svg:not([class*='size-'])]:size-3",
        sm: "h-7 gap-1.5 px-2.5 [&_svg:not([class*='size-'])]:size-3.5",
        lg: "h-9 gap-2 px-4",
        icon: "size-8",
        "icon-xs": "size-6 [&_svg:not([class*='size-'])]:size-3",
        "icon-sm": "size-7",
        "icon-lg": "size-9",
      },
    },
    defaultVariants: {
      variant: "default",
      size: "default",
    },
  }
)

function Button({
  className,
  variant = "default",
  size = "default",
  ...props
}: ButtonPrimitive.Props & VariantProps<typeof buttonVariants>) {
  return (
    <ButtonPrimitive
      data-slot="button"
      className={cn(buttonVariants({ variant, size, className }))}
      {...props}
    />
  )
}

export { Button, buttonVariants }
