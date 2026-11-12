// Starts the HUD's moving aurora (AG-2) behind `#aurora` and keeps it in step with the page: the tokens
// (--base, --wash-1..3: re-read when `hud-appearance-change` fires or `:root`'s style changes, so AG-3's
// colours apply live), Settings -> Appearance (lib/appearance.ts), `document.hidden` and
// `prefers-reduced-motion`. Without WebGL2 (or half-float targets) nothing is drawn and the still CSS wash
// stays; so it does while the GPU context is lost.
import { APPEARANCE_EVENT, loadAppearance } from '../lib/appearance';
import { chatActivity } from './activity';
import { createAuroraController, type AuroraController, type AuroraInputs } from './controller';
import type { AuroraStyle } from './keys';
import { readPalette } from './palette';
import { FrameStats, mountStatsOverlay, statsMode } from './stats';

export function startAurora(doc: Document = document): (() => void) | null {
  const host = doc.getElementById('aurora');
  if (!host) return null;
  const win = doc.defaultView;
  if (!win) return null;

  const mode = statsMode();
  const stats = mode === 'off' ? null : new FrameStats();
  const reduced = typeof win.matchMedia === 'function' ? win.matchMedia('(prefers-reduced-motion: reduce)') : null;

  const inputs = (): AuroraInputs => {
    const a = loadAppearance();
    const style: AuroraStyle = a.motion === 'flow' ? 'flow' : 'fluid';
    return {
      style,
      palette: readPalette(doc.documentElement),
      still: a.motion === 'still' || !!reduced?.matches,
      visible: !doc.hidden,
      stir: a.stir,
      activity: a.activity,
    };
  };

  const controller: AuroraController = createAuroraController(host, inputs(), {
    allowSoftwareFluid: mode === 'fluid',
    stats,
  });
  chatActivity.setSink((rect) => controller.ripple(rect), () => controller.canRipple());
  const overlay = stats ? mountStatsOverlay(doc, stats, () => controller.info()) : null;

  const refresh = () => controller.update(inputs());
  doc.addEventListener('visibilitychange', refresh);
  win.addEventListener(APPEARANCE_EVENT, refresh);
  win.addEventListener('storage', refresh); // another HUD window changed the setting
  reduced?.addEventListener?.('change', refresh);
  // AG-3 sets the tokens as inline style on :root; any such change re-reads them.
  const watch = typeof MutationObserver === 'function' ? new MutationObserver(refresh) : null;
  watch?.observe(doc.documentElement, { attributes: true, attributeFilter: ['style'] });

  return () => {
    doc.removeEventListener('visibilitychange', refresh);
    win.removeEventListener(APPEARANCE_EVENT, refresh);
    win.removeEventListener('storage', refresh);
    reduced?.removeEventListener?.('change', refresh);
    watch?.disconnect();
    chatActivity.setSink(null);
    overlay?.dispose();
    controller.destroy();
  };
}
