import { act, createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { parseSttSettings } from '../lib/sttSettings';
import { Capture } from '../lib/capture';
import { VoiceDiagnostics } from '../lib/voiceDiagnostics';

const mocks = vi.hoisted(() => ({ sttSettings: vi.fn(), setSttSettings: vi.fn(), dictationCleanupCounts: vi.fn() }));
vi.mock('../api', () => ({ api: mocks }));
import { SttSettingsPanel } from './SttSettings';
import { VoiceDiagnosticsPanel } from './VoiceDiagnostics';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
let root: Root, host: HTMLDivElement;
beforeEach(() => {
  host = document.createElement('div'); document.body.appendChild(host); root = createRoot(host);
  Object.values(mocks).forEach(m => m.mockReset());
  mocks.sttSettings.mockResolvedValue(parseSttSettings({}));
  mocks.setSttSettings.mockImplementation(async change => parseSttSettings(change));
  mocks.dictationCleanupCounts.mockResolvedValue({ off: { typed: 0, scratched: 0 }, rules: { typed: 8, scratched: 2 }, raw: { typed: 2, scratched: 0 }, model: { typed: 0, scratched: 0 }, private: 'private text' });
});
afterEach(async () => { await act(async () => root.unmount()); host.remove(); });
const radio = (name: string) => host.querySelector(`[data-testid="dictation-cleanup-${name}"]`) as HTMLInputElement;

describe('dictation cleanup', () => {
  it('defaults to Rules and saves Off and Rules through voice settings', async () => {
    await act(async () => root.render(createElement(SttSettingsPanel)));
    expect(radio('rules').checked).toBe(true);
    await act(async () => radio('off').click());
    expect(mocks.setSttSettings).toHaveBeenLastCalledWith({ dictation_cleanup: 'off' });
    expect(radio('off').checked).toBe(true);
    await act(async () => radio('rules').click());
    expect(mocks.setSttSettings).toHaveBeenLastCalledWith({ dictation_cleanup: 'rules' });
    expect(radio('rules').checked).toBe(true);
  });
  it('disables the controls while a card blocks settings', async () => {
    await act(async () => root.render(createElement(SttSettingsPanel, { blocked: true })));
    expect(radio('rules').disabled).toBe(true);
    expect(radio('off').disabled).toBe(true);
  });
  it('renders only known pass counts and rates, including a zero denominator', async () => {
    const capture = new Capture({ suppressed: () => false, muted: () => false, onLevel: () => {}, onListening: () => {}, onIdle: () => {}, onChunk: () => {}, onUtteranceEnd: () => {} });
    await act(async () => root.render(createElement(VoiceDiagnosticsPanel, { diagnostics: new VoiceDiagnostics(), capture, muted: false })));
    const block = host.querySelector('[data-testid="dictation-cleanup-counts"]')!;
    expect(mocks.dictationCleanupCounts).toHaveBeenCalledOnce();
    expect(block.textContent).toContain('rules · typed 8 · scratched 2 · scratch rate 25.0%');
    expect(block.textContent).toContain('off · typed 0 · scratched 0 · scratch rate 0.0%');
    expect(block.textContent).not.toContain('private');
    expect(block.textContent).not.toContain('NaN');
  });
});
