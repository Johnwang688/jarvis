// Path pieces for the `/`-separated paths the HUD shows (workspace-relative or
// absolute, never Windows). Plain string work: no normalising, no `.`/`..`.
// Prose parsing (`speech/prose.ts pathBasename`) is a different job and stays there.

/**
 * What follows the last `/`: `""` for `""` and for a trailing slash (`"a/"`),
 * the whole string when it holds no slash.
 */
export function basename(path: string): string {
  return path.slice(path.lastIndexOf("/") + 1);
}

/**
 * `basename`, but a path with nothing after its last `/` (`""`, `"a/"`) answers
 * with the whole path rather than `""`: a label that is never blank.
 */
export function basenameOrPath(path: string): string {
  return basename(path) || path;
}

/**
 * What precedes the last `/`; `""` when there is no slash or the only slash is
 * the first character (`"/a"` -> `""`, not `"/"`). A trailing slash is dropped
 * (`"a/"` -> `"a"`, `"a/b/"` -> `"a/b"`).
 */
export function dirname(path: string): string {
  const i = path.lastIndexOf("/");
  return i > 0 ? path.slice(0, i) : "";
}
