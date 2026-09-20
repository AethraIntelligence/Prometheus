/**
 * Which model each kind of work goes to.
 *
 * The kinds of work are the runtime's own and arrive with the defaults; nothing
 * here decides what any of them mean. Choosing "let the router decide" is a
 * real option rather than an absence: without a default the platform ranks the
 * candidates itself, which is what a fresh installation does.
 *
 * Each row offers the models that can do that kind of work, read off the
 * flags the runtime sends: an embedding model for embedding, a model that
 * writes or a model built to decide for decisions, a model that writes for
 * everything else. Offering all of them was how the document index
 * could be pointed at a chat model and planning at `nomic-embed-text`.
 */

import type { ModelEntry } from "../../../entities/provider";

interface Props {
  kinds: string[];
  defaults: Record<string, string>;
  models: ModelEntry[];
  onRoute: (taskKind: string, entryName: string) => Promise<void>;
  disabled?: boolean;
}

export function WorkRouting({
  kinds,
  defaults,
  models,
  onRoute,
  disabled,
}: Props) {
  return (
    <table className="routing" aria-label="Where work goes">
      <tbody>
        {kinds.map((kind) => (
          <tr key={kind}>
            <th scope="row">{kind.toLowerCase()}</th>
            <td>
              <select
                value={defaults[kind] ?? ""}
                onChange={(event) => void onRoute(kind, event.target.value)}
                disabled={disabled || models.length === 0}
                aria-label={`Model for ${kind.toLowerCase()}`}
              >
                <option value="">the router decides</option>
                {models
                  .filter((entry) =>
                    kind === "EMBEDDING"
                      ? entry.embeds
                      : kind === "DECISION"
                        ? entry.generates_text || entry.decides
                        : entry.generates_text,
                  )
                  .map((entry) => (
                    <option key={entry.name} value={entry.name}>
                      {entry.name}
                    </option>
                  ))}
              </select>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
