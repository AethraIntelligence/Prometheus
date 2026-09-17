import type { MemoryTrace } from "../model/types";
import { MemoryLine } from "./MemoryLine";
import { MemoryUses } from "./MemoryUses";

/** Everything a memory can be followed back to, as the core traced it. */
export function MemoryTraceView({ trace }: { trace: MemoryTrace }) {
  return (
    <div className="memory-trace">
      <ul className="memories">
        <MemoryLine item={trace.item} />
      </ul>
      {trace.item.source?.ref && (
        <p className="note">
          Source record: {trace.item.source.kind.toLowerCase()} {trace.item.source.ref}
        </p>
      )}
      {trace.superseded_by && (
        <section aria-label="Replaced by">
          <h3>Replaced by</h3>
          <ul className="memories">
            <MemoryLine item={trace.superseded_by} />
          </ul>
        </section>
      )}
      {trace.derived_from.length > 0 && (
        <section aria-label="Replaces">
          <h3>Replaces</h3>
          <ul className="memories">
            {trace.derived_from.map((item) => (
              <MemoryLine key={item.id} item={item} />
            ))}
          </ul>
        </section>
      )}
      {trace.contradicts.length > 0 && (
        <section aria-label="Disputed by">
          <h3>Disagrees with</h3>
          <ul className="memories">
            {trace.contradicts.map((item) => (
              <MemoryLine key={item.id} item={item} />
            ))}
          </ul>
        </section>
      )}
      <section aria-label="Where it was used">
        <h3>Where it was used</h3>
        <MemoryUses uses={trace.uses} empty="No run has used this memory yet." />
      </section>
    </div>
  );
}
