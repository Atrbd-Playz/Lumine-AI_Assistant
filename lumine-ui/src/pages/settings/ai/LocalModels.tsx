import { useCallback, useState } from "react";
import { discoverLocalModels, type LocalModelDiscovery } from "../../../features/settings/aiConfigClient";
import { Hint } from "../../../components/ui/hint";

/**
 * Finding the models a local server is holding.
 *
 * ## Why this is a button and not an automatic list
 *
 * The catalog is a list Lumine knows about. A model server is a list that server
 * knows about, and the two are not the same list — someone with `qwen3:8b` pulled
 * has never heard of it, and a chooser that does not offer it is answering "no" to
 * a model that is sitting right there.
 *
 * So the chooser asks the server. It does not ask on open, because that is a
 * network round trip on a screen the person may have come to for something else,
 * against a server that may not be running — and the resulting "cannot connect"
 * would be the first thing on the tab rather than a consequence of a click.
 *
 * ## The three failures are three different sentences
 *
 * Not running, running with nothing pulled, and a wrong address in the box above
 * all look like failure from inside the control. They are the three things a person
 * can do something different about, so they are reported separately rather than
 * collapsed into "could not reach your local model server".
 */

export type LocalModelsProps = {
  /** The `base_url` currently on the stage, if the profile set one. */
  baseUrl: string;
  /** Choose a model the server reported. */
  onPick: (modelId: string) => void;
  /** The model id currently selected, so it can be marked. */
  selected: string;
};

export function LocalModels({ baseUrl, onPick, selected }: LocalModelsProps) {
  const [result, setResult] = useState<LocalModelDiscovery | null>(null);
  const [busy, setBusy] = useState(false);

  const look = useCallback(async () => {
    setBusy(true);
    try {
      setResult(await discoverLocalModels(baseUrl));
    } catch {
      // The only way to land here is a desktop layer that is not there, which
      // cannot also be reached by a person clicking a button. The one useful
      // thing to say is that this needs the app.
      setResult({
        ok: false,
        baseUrl,
        route: "",
        latencyMs: 0,
        models: [],
        error: "desktop_unavailable",
        detail: "Looking for local models needs the Lumine app, not a browser tab.",
      });
    } finally {
      setBusy(false);
    }
  }, [baseUrl]);

  return (
    <section className="settings-block">
      <div className="settings-block-head flex items-center gap-1.5">
        <h2>On this computer</h2>
        <Hint label="What this is">
          A model server running on your own machine, reached over the same API shape as OpenAI. Lumine asks it
          what it has rather than guessing, because the list of models you have pulled is not a list Lumine could
          know in advance. Nothing you say to a local model leaves the computer.
        </Hint>
      </div>

      <div className="local-models flex flex-col gap-2.5 items-start">
        <button type="button" className="settings-secondary" onClick={look} disabled={busy}>
          {busy ? "Looking…" : result ? "Look again" : "Look for local models"}
        </button>

        {result && (
          <div className="local-models-result w-full flex flex-col gap-2 py-2.5 px-3 rounded-sm bg-surface-muted shadow-elev-1" role="status">
            {result.models.length > 0 ? (
              <>
                <p className="field-hint text-faint text-[11.5px] leading-[1.5]">
                  {result.models.length} model{result.models.length === 1 ? "" : "s"} at {result.baseUrl}
                  {result.route === "/v1/models" ? ", from the OpenAI-compatible list." : "."}
                </p>
                <ul className="local-models-list flex flex-col gap-0.5 m-0 p-0 list-none">
                  {result.models.map((model) => {
                    // Hoisted so the guard and the render read the same value.
                    // The old line checked `size !== undefined` and then handed
                    // it straight to a formatter built on division: a server
                    // that sends `null`, a string or a negative arrives as
                    // `NaN` or `-12 B`, neither of which is undefined, and both
                    // went on screen as a size.
                    const size = formatBytes(model.size);
                    return (
                      <li key={model.id}>
                        <button
                          type="button"
                          className={model.id === selected ? "is-current" : ""}
                          aria-current={model.id === selected || undefined}
                          onClick={() => onPick(model.id)}
                        >
                          <span className="local-model-id min-w-0 overflow-hidden text-ellipsis whitespace-nowrap font-mono">{model.id}</span>
                          {size !== null && <span className="local-model-size">{size}</span>}
                        </button>
                      </li>
                    );
                  })}
                </ul>
                {selected && !result.models.some((model) => model.id === selected) && (
                  <p className="field-hint text-faint text-[11.5px] leading-[1.5]">
                    {selected} is selected but this server is not serving it. It may have been pulled somewhere
                    else, or removed.
                  </p>
                )}
              </>
            ) : (
              <p className="field-hint text-faint text-[11.5px] leading-[1.5]">{result.detail || "No models were reported."}</p>
            )}
          </div>
        )}
      </div>
    </section>
  );
}

/**
 * A model size in the units somebody would use out loud.
 *
 * Decimal, not binary. `ollama pull` prints `5207029248` and the community says
 * "5.2 GB" about it, so matching that is the difference between a number that
 * answers "will this fit" and one that does not. A 1.0 GB and a 1.07 GB model are
 * the same disk to everybody and different numbers here.
 */
function formatBytes(bytes: number | undefined): string | null {
  // The units below are arithmetic, and arithmetic on a value the server chose
  // does not fail loudly — it produces `NaN`, which then prints. So the shape is
  // checked first and an unusable size renders as *no* size, which is the only
  // honest answer to a number we cannot read.
  if (typeof bytes !== "number" || !Number.isFinite(bytes) || bytes < 0) return null;
  const gigabytes = bytes / 1e9;
  if (gigabytes >= 1) return `${gigabytes.toFixed(1)} GB`;
  const megabytes = bytes / 1e6;
  if (megabytes >= 1) return `${Math.round(megabytes)} MB`;
  return `${bytes} B`;
}
