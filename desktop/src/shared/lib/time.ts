/**
 * When something happened, in as few words as carry the meaning.
 *
 * Today is a time, this year is a day and a month, anything older carries its
 * year. A full timestamp on every row is eighteen characters of which three
 * matter, and a person scanning a list is asking "recently or not", never "at
 * which second".
 *
 * The local clock, because the person reading it is here. The core stores UTC.
 */

export function moment(iso: string, now: Date = new Date()): string {
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return "";
  const sameDay =
    at.getFullYear() === now.getFullYear() &&
    at.getMonth() === now.getMonth() &&
    at.getDate() === now.getDate();
  if (sameDay) {
    return at.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }
  return at.toLocaleDateString([], {
    day: "numeric",
    month: "short",
    year: at.getFullYear() === now.getFullYear() ? undefined : "numeric",
  });
}
