import { Hint } from "../../../components/ui/hint";

/**
 * One labelled setting: a name, the control, and a line of help at most.
 *
 * The label is a `<span>`, not a `<label>`, because the control it names is not
 * always a native form element. A dropdown is a button and a slider is a div with
 * a role, and a `<label for>` pointing at either does nothing; the accessible name
 * travels on the control itself. Wrapping the pair in a `<label>` was the
 * alternative and it makes clicking the text toggle a control nobody can see the
 * bounds of.
 *
 * So there is exactly one shape for a named setting in this app, whether the
 * control behind it is a dropdown, a slider or a text box.
 */
export type FieldProps = {
  label: string;
  /** The `?` tooltip. Shown only when there is something to add. */
  help?: string;
  /** A line rendered under the control. Use for a caveat, not a restatement. */
  hint?: string;
  children: React.ReactNode;
  className?: string;
};

export function Field({ label, help, hint, children, className }: FieldProps) {
  return (
    <div className={"field" + (className ? ` ${className}` : "")}>
      <div className="field-row flex gap-2" style={{ alignItems: "center" }}>
        <span className="field-label text-soft text-[12px]">{label}</span>
        {help && <Hint>{help}</Hint>}
      </div>
      {children}
      {hint && <small className="field-hint text-faint text-[11.5px] leading-[1.5]">{hint}</small>}
    </div>
  );
}
