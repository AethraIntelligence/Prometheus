import type { PluginIcon } from "../model/plugin";

/**
 * A plugin's mark: its drawing on its tile, or the first letter where it has none.
 *
 * Path data rendered as an element, never markup and never an image - the
 * window loads no image it did not ship, and the runtime has already reduced
 * the icon file to numbers and path commands.
 */
export function PluginMark({
  name,
  icon,
  size = "row",
}: {
  name: string;
  icon: PluginIcon | null;
  size?: "row" | "tile" | "large";
}) {
  if (!icon) {
    return (
      <span className={`plugin-mark ${size} letter`} aria-hidden="true">
        {name.trim().charAt(0).toUpperCase() || "?"}
      </span>
    );
  }
  return (
    <span
      className={`plugin-mark ${size}`}
      style={{ background: icon.background, color: icon.color }}
      aria-hidden="true"
    >
      <svg viewBox={icon.view_box} fill="currentColor">
        {icon.paths.map((d, index) => (
          <path key={index} d={d} />
        ))}
      </svg>
    </span>
  );
}
