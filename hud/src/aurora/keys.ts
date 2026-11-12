// Which animation runs (AG-2). `fluid`: the fluid simulation (default; stirred by the pointer and by chat
// activity). `flow`: the lighter flow shader. The owner's third choice, Still, is a Motion setting
// (lib/appearance.ts) that draws one frame, not a style.
export const AURORA_STYLES = ['fluid', 'flow'] as const;
export type AuroraStyle = (typeof AURORA_STYLES)[number];
