import { ContextPanel } from "./home/components/ContextPanel";
import type { LumineState } from "./home/types";

/**
 * Right-side route for workspace context.
 * The shell decides when this route is visible; this file only owns its entry point.
 */
export default function Context({ state, onSettings }: { state: LumineState; onSettings: () => void }) {
  return <ContextPanel state={state} onSettings={onSettings} />;
}
