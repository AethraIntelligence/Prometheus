/**
 * The brand's images, bundled with the window.
 *
 * Files rather than inline SVG, unlike the marks in `shared/ui/icons`: the logo
 * is an engraving of a few hundred curves, and a component holding it would be
 * sixty kilobytes of path data in the middle of the source. The masters live
 * in the website's repository (`assets/brand/`); these are copies sized for
 * the window.
 */

export { default as logoUrl } from "./logo.svg";
export { default as avatarUrl } from "./avatar.png";
