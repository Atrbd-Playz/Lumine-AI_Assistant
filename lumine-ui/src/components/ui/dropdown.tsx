import { useEffect, useId, useRef, useState } from "react";
import { Select } from "@base-ui/react/select";
import { CaretDown, PencilSimple } from "@phosphor-icons/react";

/**
 * A dropdown menu, in Lumine's colours.
 *
 * ## Why this exists
 *
 * The brief was "use dropdown menus" for the settings, and a native `<select>`
 * was the obvious answer right up until it wasn't. The problem is not that a
 * native select is a bad control — it is a good one, and it works on every
 * platform for free. The problem is that its popup is drawn by the OS: on Windows
 * it is a bright system panel that ignores the theme entirely, so a settings
 * screen that is otherwise warm and low-contrast gets a hard white rectangle the
 * moment someone opens it. That reads as a different application.
 *
 * So this is base-ui's listbox with Lumine's surfaces. Same keyboard model, same
 * typeahead, and a popup that belongs to the same app.
 *
 * ## One control, everywhere
 *
 * Every chooser in the app is this: the palette, the typeface, the bubble style,
 * the stage's model, the voice, the reasoning level. A settings screen that
 * mixes a native select, a datalist, a segmented button row and a wall of cards
 * does not read as four settings; it reads as four different applications. The
 * sameness is the feature.
 *
 * ## Value semantics
 *
 * The empty string means "let the provider decide" wherever it is offered, and
 * this maps that to `null` — base-ui's own "nothing selected" — so a dropdown can
 * show a real placeholder instead of a fake first row pretending to be an option.
 * A stage that sends nothing is the only choice guaranteed to be valid for the
 * whichever model is selected, so it has to be expressible.
 */
export type DropdownProps = {
  /** Accessible name. Also used as the trigger's visible label when set. */
  label: string;
  /** The chosen value, or `""` for the provider default. */
  value: string;
  onChange: (value: string) => void;
  /**
   * The choices. A value of `""` is not a choice — it is expressed by
   * `placeholder`, so a caller can never offer two different "default" rows.
   */
  options: ReadonlyArray<{
    value: string;
    label: string;
    hint?: string;
    /**
     * A CSS colour shown as a dot before the label, and repeated in the trigger.
     *
     * For the lists whose items are identified by how they look rather than by
     * what they are called: a saved palette is "Sakura" and also pink, and a
     * dropdown of twelve names with no colour in it is a dropdown of twelve
     * things to try in turn.
     */
    swatch?: string;
    disabled?: boolean;
    /**
     * Renders the row under a small caption, with rows of the same group kept
     * together.
     *
     * For the list whose real question is "which one of these", and whose
     * options have a natural parent: models belong to a provider, and browsing
     * them as one list beats picking a provider and then a model, because the
     * first of those two questions usually has an obvious answer.
     */
    group?: string;
    /**
     * A CSS `font-family` for the row's label, in the popup *and* in the closed
     * trigger.
     *
     * The typeface list is the one chooser whose options are identified by how
     * they look rather than by what they are called. "Newsreader" and
     * "Roboto" are names nobody can hold in their head long enough to choose
     * between, and the wall of cards that used to answer this — each one set in
     * its own face, six of them, a third of the settings page — was the correct
     * idea at the wrong scale. Six rows in a menu, each showing its own
     * typeface, keeps the sample and loses the block.
     *
     * The description stays in the interface face, because the point of the hint
     * ("Editorial warmth") is to describe the face rather than demonstrate it.
     */
    face?: string;
  }>;
  placeholder?: string;
  disabled?: boolean;
  className?: string;
  id?: string;
  /**
   * Label for a trailing row that swaps the trigger for a text field.
   *
   * Some lists are curated and some are not. A voice list holds a handful of
   * voices we chose; the provider publishes hundreds more, and a cloned voice has
   * no name we could list at all. Without this, the id is only reachable by
   * editing a saved profile on disk, which is a worse answer than the datalist it
   * replaced. The row is inside the same list, so the control keeps one shape.
   */
  editable?: string;
};

/** The value of the synthetic row that opens the text field. */
const CUSTOM = "custom-value";

export function Dropdown({
  label,
  value,
  onChange,
  options,
  placeholder = "Choose…",
  disabled,
  className,
  id,
  editable,
}: DropdownProps) {
  // The trigger becomes a text field. Local state, not `value`, because the parent
  // only hears about a value once it is committed — writing every keystroke up
  // would put a half-typed voice id into the profile and then reject the stage
  // that has to carry it.
  const [typing, setTyping] = useState(false);
  const [draft, setDraft] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  const fieldId = useId();

  useEffect(() => {
    if (typing) inputRef.current?.focus();
  }, [typing]);

  // A value with no row to show it renders as an empty control, which reads as
  // "nothing is set" when in fact something is. An uncatalogued value — a cloned
  // voice, a provider that added a model — is therefore shown as itself.
  const rows =
    value !== "" && !options.some((option) => option.value === value)
      ? [...options, { value, label: value, hint: "Not in the published list" }]
      : options;

  const commit = (next: string) => {
    setTyping(false);
    const trimmed = next.trim();
    // Empty means the provider decides, which is the same state as the unset
    // value, so it is a change rather than a no-op.
    if (trimmed !== value) onChange(trimmed);
  };

  if (typing) {
    return (
      <input
        ref={inputRef}
        id={id ?? fieldId}
        aria-label={label}
        className="h-9 w-full rounded-sm bg-surface-muted px-3 text-[13px] text-foreground shadow-elev-1 outline-none placeholder:text-faint focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
        value={draft}
        placeholder={placeholder}
        spellCheck={false}
        autoComplete="off"
        onChange={(event) => setDraft(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter") {
            event.preventDefault();
            commit(draft);
          }
          if (event.key === "Escape") {
            event.preventDefault();
            event.stopPropagation();
            setTyping(false);
          }
        }}
        onBlur={() => commit(draft)}
      />
    );
  }

  // The map base-ui reads to label a chosen value and to drive typeahead. The
  // placeholder is absent from it on purpose: base-ui falls back to `placeholder`
  // for a null value, which is exactly the "provider default" case.
  const items = Object.fromEntries(rows.map((option) => [option.value, option.label]));
  const chosen = rows.find((option) => option.value === value);
  // A function child, so the closed trigger can set the label in the row's own
  // typeface. The plain-text label is what base-ui's typeahead matches against,
  // which is why the face is applied to the element rather than baked into the
  // string.
  const labelFor = (raw: unknown) => {
    const row = rows.find((option) => option.value === raw);
    if (!row) return <span className="text-faint">{placeholder}</span>;
    return <span style={row.face ? { fontFamily: row.face } : undefined}>{row.label}</span>;
  };

  return (
    <Select.Root
      items={items}
      value={value === "" ? null : value}
      onValueChange={(next) => {
        const picked = next === null ? "" : String(next);
        if (picked === CUSTOM) {
          setDraft(value);
          setTyping(true);
          return;
        }
        onChange(picked);
      }}
      disabled={disabled}
    >
      <Select.Trigger
        id={id}
        aria-label={label}
        className={
          // Depth 1, not a border. The trigger is a surface sitting on a panel, and
          // the shadow is what says so. `data-popup-open` lifts it to depth 2 while
          // the menu is out, so the trigger reads as the anchor it now is.
          "flex h-9 w-full items-center justify-between gap-2 rounded-sm bg-surface-muted px-3 " +
          "text-left text-[13px] text-foreground shadow-elev-1 transition-shadow " +
          "hover:text-foreground data-[popup-open]:shadow-elev-2 " +
          "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent " +
          "disabled:cursor-not-allowed disabled:opacity-50 " +
          (className ?? "")
        }
      >
        {/* The swatch is rendered outside `Select.Value` because base-ui owns that
            node's text. Duplicating the dot in the trigger is what keeps the
            closed control identifiable at a glance, which is the whole point of
            having swatches in a list at all. */}
        {chosen?.swatch && (
          <span
            aria-hidden="true"
            className="size-3 shrink-0 rounded-full shadow-elev-1"
            style={{ background: chosen.swatch }}
          />
        )}
        <Select.Value className="min-w-0 truncate">{labelFor}</Select.Value>
        <Select.Icon className="shrink-0 text-faint">
          <CaretDown size={11} weight="bold" />
        </Select.Icon>
      </Select.Trigger>
      <Select.Portal>
        <Select.Positioner
          sideOffset={6}
          alignItemWithTrigger={false}
          className="z-50 max-h-[16rem] transition-[opacity,transform] duration-120 ease-out"
        >
          <Select.Popup
            className={
              // Depth 3: this floats over the settings dialog itself, and the extra
              // separation is what stops the list reading as part of the panel
              // behind it.
              "max-h-[16rem] overflow-y-auto overscroll-contain rounded-sm bg-popover p-1 " +
              "text-popover-foreground shadow-elev-3"
            }
          >
            <Select.List>{renderRows(rows, editable)}</Select.List>
          </Select.Popup>
        </Select.Positioner>
      </Select.Portal>
    </Select.Root>
  );
}

type Row = DropdownProps["options"][number];

const ITEM_CLASS =
  "flex cursor-default flex-col rounded-[6px] px-2.5 py-1.5 text-[13px] outline-none " +
  "text-popover-foreground data-[highlighted]:bg-accent/12 data-[highlighted]:text-accent " +
  "data-[selected]:text-accent data-[disabled]:pointer-events-none data-[disabled]:opacity-45";

function OptionRow({ option }: { option: Row }) {
  return (
    <Select.Item value={option.value} disabled={option.disabled} className={ITEM_CLASS}>
      <span className="flex items-center gap-2">
        {option.swatch && (
          <span
            aria-hidden="true"
            className="size-3 shrink-0 rounded-full shadow-elev-1"
            style={{ background: option.swatch }}
          />
        )}
        <Select.ItemText style={option.face ? { fontFamily: option.face } : undefined}>{option.label}</Select.ItemText>
      </span>
      {option.hint && <span className="text-[11.5px] leading-snug text-faint">{option.hint}</span>}
    </Select.Item>
  );
}

/**
 * The list's rows, grouped when they declare a group and flat when they do not.
 *
 * Consecutive rows sharing a caption become one `Select.Group`, which is what
 * makes the arrow keys and typeahead treat them as a family, and keeps a family's
 * rows together when the list scrolls.
 */
function renderRows(rows: ReadonlyArray<Row>, editable: string | undefined) {
  if (!rows.some((option) => option.group !== undefined)) {
    return (
      <>
        {rows.map((option) => (
          <OptionRow key={option.value} option={option} />
        ))}
        {editable && <CustomRow label={editable} />}
      </>
    );
  }

  // Bucketed rather than emitted in order, because a `Select.Group` has to wrap
  // its own items. Rows with no caption are their own bucket, keyed by their own
  // value, so an ungrouped row is never swept under a neighbouring caption.
  const buckets: { caption: string | null; items: Row[] }[] = [];
  for (const option of rows) {
    const last = buckets[buckets.length - 1];
    const caption = option.group ?? null;
    if (last && caption === last.caption) last.items.push(option);
    else buckets.push({ caption, items: [option] });
  }

  return (
    <>
      {buckets.map((bucket) => (
        <Select.Group key={bucket.caption ?? `solo-${bucket.items[0].value}`}>
          {bucket.caption && (
            <Select.GroupLabel className="px-2.5 pb-1 pt-2 text-[10px] font-medium uppercase tracking-[0.07em] text-faint first:pt-1">
              {bucket.caption}
            </Select.GroupLabel>
          )}
          {bucket.items.map((option) => (
            <OptionRow key={option.value} option={option} />
          ))}
        </Select.Group>
      ))}
      {editable && <CustomRow label={editable} />}
    </>
  );
}

/** The row that turns the trigger into a text field. */
function CustomRow({ label }: { label: string }) {
  return (
    <Select.Item
      value={CUSTOM}
      className="mt-1 flex cursor-default items-center gap-2 rounded-[6px] border-t border-[var(--color-border)] px-2.5 pt-2 text-[13px] text-faint outline-none data-[highlighted]:text-accent"
    >
      <PencilSimple size={12} />
      <Select.ItemText>{label}</Select.ItemText>
    </Select.Item>
  );
}
