/**
 * How long one line is kept. The choices are the person's; what "until
 * replaced" means - no expiry, superseded by a later statement - is the core's.
 */
const CHOICES: { label: string; days: number | null }[] = [
  { label: "Keep until replaced", days: null },
  { label: "Keep 30 days", days: 30 },
  { label: "Keep 7 days", days: 7 },
];

export function KeepMemory({
  id,
  expiresAt,
  onKeep,
}: {
  id: string;
  expiresAt: string;
  onKeep: (id: string, days: number | null) => Promise<void>;
}) {
  return (
    <select
      className="keep"
      aria-label="How long to keep this"
      value={expiresAt ? "" : "forever"}
      onChange={(event) => {
        const chosen = CHOICES.find((choice) => String(choice.days ?? "forever") === event.target.value);
        if (chosen) void onKeep(id, chosen.days);
      }}
    >
      {expiresAt && <option value="">Kept until {expiresAt.slice(0, 10)}</option>}
      {CHOICES.map((choice) => (
        <option key={choice.label} value={String(choice.days ?? "forever")}>
          {choice.label}
        </option>
      ))}
    </select>
  );
}
