import { useState } from "react";
import type { CatalogModel, ProviderCatalog } from "../../../features/settings/aiConfigTypes";
import { Icon } from "../../home/components/Icon";

/**
 * What Lumine can hand each model, as a matrix.
 *
 * ## Why a matrix and not a list of sentences
 *
 * The question this answers is comparative — "which of these can see?" — and a
 * list of sentences makes the reader do the comparison themselves, one model at a
 * time. A row per model and a column per kind of input puts the answer next to
 * the thing being compared, and an empty cell is legible without reading
 * anything.
 *
 * ## Hidden rather than absent
 *
 * `aria-hidden` on the dots with a real text alternative on the row: a screen
 * reader announcing "text, dot, audio, dot" is worse than useless, and the row's
 * own label already carries the answer in words.
 */
const COLUMNS = [
  { id: "text", label: "Text" },
  { id: "audio", label: "Audio" },
  { id: "image", label: "Image" },
  { id: "video", label: "Video" },
] as const;

/** The stage a model serves, in the order a person meets them. */
const STAGE_ORDER = ["realtime", "stt", "llm", "tts", "vad"] as const;

const STAGE_LABEL: Record<string, string> = {
  realtime: "Realtime",
  stt: "Speech to text",
  llm: "Language",
  tts: "Speech",
  vad: "Turn detection",
  transport: "Transport",
};

type Row = { provider: string; model: CatalogModel };

export function CapabilityMatrix({ catalog }: { catalog: ProviderCatalog }) {
  const [showRetired, setShowRetired] = useState(false);

  const rows: Row[] = [];
  for (const provider of catalog.providers) {
    for (const model of provider.models) {
      // A transport carries other stages' output; it is not itself given
      // anything, so a row of empty cells would be noise.
      if (model.capability === "transport") continue;
      if (!showRetired && model.status === "retired") continue;
      rows.push({ provider: provider.label, model });
    }
  }
  rows.sort((a, b) => {
    const stageA = STAGE_ORDER.indexOf(a.model.capability as (typeof STAGE_ORDER)[number]);
    const stageB = STAGE_ORDER.indexOf(b.model.capability as (typeof STAGE_ORDER)[number]);
    if (stageA !== stageB) return stageA - stageB;
    return a.model.label.localeCompare(b.model.label);
  });

  const retired = catalog.providers.reduce(
    (total, provider) => total + provider.models.filter((model) => model.status === "retired").length,
    0,
  );
  const seeing = rows.filter((row) => row.model.inputModalities.includes("image"));

  return (
    <>
      {seeing.length > 0 && (
        <p className="validation is-ok">
          <Icon name="check" size={15} />
          {seeing.length === 1
            ? `${seeing[0].model.label} takes a camera feed. A camera can only be turned on with a realtime stack.`
            : `${seeing.length} models take a camera feed. A camera can only be turned on with a realtime stack.`}
        </p>
      )}

      <div className="capability-scroll">
        <table className="capability-matrix">
          <caption className="sr-only">
            What Lumine can hand each model, by kind of input.
          </caption>
          <thead>
            <tr>
              <th scope="col">Model</th>
              <th scope="col">Stage</th>
              {COLUMNS.map((column) => (
                <th scope="col" key={column.id}>
                  {column.label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map(({ provider, model }) => (
              <tr key={`${provider}-${model.id}`} className={model.status === "retired" ? "is-retired" : undefined}>
                <th scope="row">
                  <span className="capability-model block text-foreground text-[13px] leading-[1.3]">{model.label}</span>
                  <span className="capability-provider block mt-px text-faint text-[11px]">{provider}</span>
                </th>
                <td className="capability-stage text-soft text-[12px] whitespace-nowrap">{STAGE_LABEL[model.capability] ?? model.capability}</td>
                {COLUMNS.map((column) => {
                  const yes = model.inputModalities.includes(column.id);
                  return (
                    <td key={column.id} className="capability-cell text-center">
                      {yes ? (
                        <>
                          <span className="capability-dot" aria-hidden="true" />
                          <span className="sr-only">accepts {column.label.toLowerCase()}</span>
                        </>
                      ) : (
                        <span className="sr-only">no {column.label.toLowerCase()}</span>
                      )}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {retired > 0 && (
        <button type="button" className="capability-more" onClick={() => setShowRetired((open) => !open)}>
          {showRetired ? "Hide retired models" : `Show ${retired} retired model${retired === 1 ? "" : "s"}`}
        </button>
      )}
    </>
  );
}
