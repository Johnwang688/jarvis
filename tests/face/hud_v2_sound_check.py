"""BR10 owner sound controls against a mock daemon; no Pulse or host Chrome."""
from __future__ import annotations
import json, sys
from pathlib import Path
REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path: sys.path.insert(0, str(REPO))
from tests.face.hud_v2_layout_check import _boot, _pane, _set_ws, _set_storage, _ws, _approval


def sound_checks(browser, mock, base, check, until, guard, init_script):
    print('\nBrowser sound: owner Settings, actual readback and pending state')
    ctx = browser.new_context(viewport={'width': 1600, 'height': 1000}, permissions=['microphone'])
    guard(ctx); ctx.add_init_script(init_script)
    state = {'desired': False, 'generation': 1, 'available': True, 'reason': None}
    rows = []; patches = []; malformed = [False]
    def status(route):
        response = route.fetch(); value = response.json()
        value['browser'] = {'sound': dict(state), 'instances': list(rows)}
        if malformed[0]: value['browser']['sound']['generation'] = 'private backend detail'
        route.fulfill(response=response, json=value)
    def settings(route):
        if route.request.method != 'PATCH': return route.fallback()
        body = route.request.post_data_json; patches.append(body)
        state['desired'] = body['sound']; state['generation'] += 1
        rows[:] = [{'profile': 'p-11111111', 'sound': {'applied': not body['sound'], 'generation': 1,
                  'phase': 'pending', 'reason': 'busy'}}]
        route.fulfill(json={'sound': body['sound']})
    ctx.route(base + '/status', status); ctx.route(base + '/browser/settings', settings)
    page = ctx.new_page(); page.set_default_timeout(5000)
    errors = []; page.on('pageerror', lambda e: errors.append(str(e)))
    try:
        _boot(page, mock, base, until)
        raw = {'preset': 'cols2', 'focused': 2, 'panes': [{'view': 'chat'}, {'view': 'browser',
            'browserProfile': 'p-11111111', 'see': False, 'browser': {'active': 'pageaa', 'tabs': [
                {'id': 'pageaa', 'url': '', 'mode': 'direct', 'source': {'kind': 'unknown'}}]}},
            {'view': 'file'}, {'view': 'task'}]}
        _set_storage(page, zoom='100', layout='{}'); _set_ws(page, json.dumps(raw)); _boot(page, mock, None, until, reload=True)
        speaker = page.locator(_pane(2, '[data-testid="browser-sound-open"]'))
        until(lambda: 'Waiting' not in (speaker.get_attribute('title') or ''), timeout=4)
        check('BR10: Direct speaker explains that Direct audio is unchanged', 'Direct audio is unchanged' in speaker.get_attribute('aria-label'))
        check('BR10: discovery never writes the preference', not patches)
        chat = page.locator(_pane(1, '[data-testid="input"]')); chat.fill('sound fixture draft')
        chat.evaluate('e=>window.__soundDraftNode=e')
        speaker.focus(); speaker.press('Enter')
        switch = page.locator('[data-testid="browser-sound-setting"]')
        until(lambda: switch.count() == 1 and switch.is_enabled(), timeout=4)
        check('BR10: keyboard speaker opens Browser section without toggling', page.locator('[data-testid="settings-browser"]').get_attribute('aria-current') == 'page' and not patches and not switch.is_checked())
        check('BR10: Settings covers a still-mounted workspace', page.locator('.workspace-surface').evaluate('e=>e.inert') and chat.evaluate('e=>e===window.__soundDraftNode'))
        check('BR10: sound explanation separates Direct audio from Remote', 'Direct pages use this window' in page.locator('[data-testid="settings-content"]').inner_text())
        switch.click(); until(lambda: switch.is_checked() and switch.is_enabled(), timeout=4)
        statusline = page.locator('[data-testid="browser-sound-status"]')
        check('BR10: one explicit switch gesture sends only the sound field', patches == [{'sound': True}], str(patches))
        check('BR10: desired on does not claim applied sound while work is pending', statusline.inner_text() == 'Sound will change when current Browser work finishes.')
        rows[0] = {'profile': 'private', 'sound': {'applied': False, 'generation': 1, 'phase': 'pending', 'reason': 'private'}}
        mock.emit('browser_sound_changed', {'generation': 2})
        until(lambda: 'Close all Private panes' in statusline.inner_text(), timeout=4)
        check('BR10: Private pending requires closing all Private panes', statusline.inner_text() == 'Close all Private panes to apply sound.' and switch.is_checked())
        _approval(mock, 'sound-card'); until(lambda: page.locator('[data-testid="approval-card"]').count() == 1, timeout=4)
        check('BR10: approval blocks the sound switch and Settings close', switch.is_disabled() and page.locator('[data-testid="settings-close"]').is_disabled())
        page.keyboard.press('Escape'); until(lambda: page.locator('[data-testid="approval-card"]').count() == 0, timeout=4)
        check('BR10: card Escape denies without closing Settings or changing sound', page.locator('[data-testid="settings-page"]').count() == 1 and patches == [{'sound': True}])
        malformed[0] = True; mock.emit('browser_sound_changed', {'generation': 3})
        until(lambda: page.get_by_role('button', name='Retry sound status', exact=True).count() == 1, timeout=4)
        check('BR10: malformed status disables mutation with a fixed error', switch.is_disabled() and statusline.inner_text() == 'The sound setting could not be read. Retry to check its current state.' and 'private backend detail' not in page.locator('[data-testid="settings-content"]').inner_text())
        malformed[0] = False; rows.clear()
        page.get_by_role('button', name='Retry sound status', exact=True).click()
        until(lambda: switch.is_enabled(), timeout=4)
        check('BR10: retry reads actual state without writing', switch.is_checked() and patches == [{'sound': True}])
        page.set_viewport_size({'width': 375, 'height': 812})
        check('BR10: narrow Settings keeps the sound switch reachable', switch.is_visible() and switch.evaluate('e=>{let r=e.getBoundingClientRect();return r.left>=0&&r.right<=innerWidth}'))
        check('BR10: narrow Settings has no horizontal overflow', page.locator('[data-testid="settings-page"]').evaluate('e=>e.scrollWidth<=e.clientWidth+1'))
        page.set_viewport_size({'width': 1600, 'height': 1000})
        page.locator('[data-testid="settings-close"]').click(); until(lambda: page.locator('[data-testid="settings-page"]').count() == 0, timeout=4)
        check('BR10: closing Settings restores keyboard focus to the speaker', speaker.evaluate('e=>e===document.activeElement'))
        check('BR10: draft and protected Browser source/see survive Settings', chat.input_value() == 'sound fixture draft' and chat.evaluate('e=>e===window.__soundDraftNode') and _ws(page)['ws']['panes'][1]['browser']['tabs'][0]['source'] == {'kind': 'unknown'} and not _ws(page)['ws']['panes'][1]['see'])
        # A Remote speaker must use the same navigation without publishing owner focus.
        from tests.face.hud_v2_remote_check import Remote
        remote = Remote(ctx, mock)
        raw['panes'][1]['browser']['tabs'][0].update(mode='remote', url='https://example.test/sound')
        _set_ws(page, json.dumps(raw)); _boot(page, mock, None, until, reload=True)
        until(lambda: page.locator(_pane(2, 'canvas')).get_attribute('data-frame') == 'ready', timeout=5)
        before = len(remote.inputs()); writes = len(patches)
        speaker.focus(); speaker.press('Enter'); until(lambda: switch.count() == 1, timeout=4)
        check('BR10: Remote speaker does not send page input/focus or toggle', len(remote.inputs()) == before and len(patches) == writes, str(remote.inputs()[before:]))
        check('BR10: Remote sound opens the same authoritative Settings state', switch.is_checked())
        check('BR10: no page errors', not errors, str(errors[:3]))
        check('BR10: no control listener request', not mock.api_hits, str(mock.api_hits[:3]))
    finally:
        ctx.close()


def main():
    from playwright.sync_api import sync_playwright
    from tests.face import hud_v2_check as v2
    from tests.face.hud_v2_mock import MockDaemon
    mock = MockDaemon(0).start()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=['--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream'])
            try: sound_checks(browser, mock, f'http://127.0.0.1:{mock.port}', v2.check, v2.until, v2.guard, v2.FAKE_RECOGNIZER)
            except Exception as exc: v2.check('Sound scenario completes', False, str(exc))
            finally: browser.close()
    finally: mock.stop()
    print(f'{v2.CHECKS} sound checks, {len(v2.FAILURES)} failures')
    return bool(v2.FAILURES)
if __name__ == '__main__': sys.exit(main())