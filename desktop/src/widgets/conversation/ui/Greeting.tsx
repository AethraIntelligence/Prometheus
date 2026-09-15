/**
 * The first thing in an empty window.
 *
 * A greeting and a question, and nothing else - no configuration, no counters,
 * no list of what the platform can do. The window's job is to get one sentence
 * from the person; everything the platform is capable of sits behind that
 * sentence, which is the point of having a manager at all.
 */

import { logoUrl } from "../../../shared/assets";

export function greetingFor(hour: number): string {
  if (hour < 5) return "Good evening.";
  if (hour < 12) return "Good morning.";
  if (hour < 18) return "Good afternoon.";
  return "Good evening.";
}

export function Greeting({ now = new Date() }: { now?: Date }) {
  return (
    <div className="greeting">
      <img className="greeting-mark" src={logoUrl} alt="" />
      <h1>{greetingFor(now.getHours())}</h1>
      <p>What would you like me to do?</p>
    </div>
  );
}
