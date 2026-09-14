import type { ProviderAdvice, ProviderGuide } from "../../../entities/provider";

const VERDICT: Record<ProviderAdvice["verdict"], string> = {
  RECOMMENDED: "Recommended",
  SUPPORTED: "Supported",
  NOT_YET: "Not available yet",
};

/** Which provider to choose, as the runtime advises. The verdict is its word, not ours. */
export function ProviderAdviceList({ guide }: { guide: ProviderGuide }) {
  return (
    <ul className="advice">
      {guide.providers.map((item) => (
        <li key={item.kind} className={item.verdict === "NOT_YET" ? "muted" : undefined}>
          <div className="setup-model-head">
            <strong>{item.label}</strong>
            <span className={item.verdict === "RECOMMENDED" ? "badge" : "badge quiet"}>
              {VERDICT[item.verdict] ?? item.verdict}
            </span>
          </div>
          <p className="note">{item.text}</p>
        </li>
      ))}
    </ul>
  );
}

/** What a model has to be able to do on this platform, in plain words. */
export function RequirementsList({ guide }: { guide: ProviderGuide }) {
  return (
    <ul className="advice">
      {guide.requirements.map((item) => (
        <li key={item.title}>
          <strong>{item.title}</strong>
          <p className="note">{item.text}</p>
        </li>
      ))}
    </ul>
  );
}
