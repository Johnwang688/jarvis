"""Shared read-only computed-style judge for v1 and owner Browser scans.

This module owns no browser, session, thread or mutable page state. Keep the
JavaScript shared: v1's mutation-based snapshot and v2's isolated read use
the same hidden-text rules.
"""

_JUDGE_JS = r"""
    const se = document.scrollingElement || document.documentElement;
    const minX = getComputedStyle(document.documentElement).direction === 'rtl'
        ? Math.min(0, se.clientWidth - se.scrollWidth) : 0;
    const sx = window.scrollX, sy = window.scrollY;
    const styles = new Map();
    const css = (e) => {
        let s = styles.get(e);
        if (!s) { s = getComputedStyle(e); styles.set(e, s); }
        return s;
    };
    const moving = (e, cs, prop) => {
        const props = cs.transitionProperty.split(',').map((s) => s.trim());
        const durs = cs.transitionDuration.split(',').map(parseFloat);
        for (let i = 0; i < props.length; i++)
            if ((props[i] === prop || props[i] === 'all') && durs[i % durs.length] > 0) return true;
        try {
            return e.getAnimations().some((a) => a.playState === 'running' || a.playState === 'pending');
        } catch (_) { return false; }
    };
    const faint = (e, cs) => {
        if (parseFloat(cs.opacity) <= 0.05 && !moving(e, cs, 'opacity')) return true;
        const m = /opacity\(([^)]*)\)/.exec(cs.filter || '');
        if (!m || !m[1].trim()) return false;
        const v = /%\s*$/.test(m[1]) ? parseFloat(m[1]) / 100 : parseFloat(m[1]);
        return v <= 0.05 && !moving(e, cs, 'filter');
    };
    const clipRect = (cs) => {
        if (cs.position !== 'absolute' && cs.position !== 'fixed') return false;
        const m = /^rect\((.*)\)$/.exec(cs.clip || '');
        if (!m) return false;
        const n = m[1].split(/[\s,]+/).map(parseFloat);
        return n.length === 4 && n.every(Number.isFinite)
            && (n[1] - n[3] <= 1 || n[2] - n[0] <= 1);
    };
    const clipPath = (v) => {
        if (!v || v === 'none') return false;
        let m = /^(circle|ellipse)\(([^)]*)\)/.exec(v);
        if (m) {
            // circle(0), or an ellipse with either radius zero: no area.
            const radii = m[2].split(' at ')[0].trim().split(/\s+/).slice(0, m[1] === 'circle' ? 1 : 2);
            return radii.some((x) => x !== '' && parseFloat(x) === 0);
        }
        m = /^polygon\((?:\s*(?:nonzero|evenodd)\s*,)?([^)]*)\)/.exec(v);
        if (m) {
            const pts = m[1].split(',').map((p) => p.trim().split(/\s+/));
            if (pts.some((p) => p.length !== 2)) return false;
            const units = new Set(pts.flat().filter((s) => parseFloat(s) !== 0)
                .map((s) => (s.endsWith('%') ? '%' : 'px')));
            if (units.size > 1) return false;
            let a = 0;
            for (let i = 0; i < pts.length; i++) {
                const [x1, y1] = pts[i].map(parseFloat);
                const [x2, y2] = pts[(i + 1) % pts.length].map(parseFloat);
                a += x1 * y2 - x2 * y1;
            }
            return pts.length < 3 || a === 0;
        }
        m = /^inset\(([^)]*)\)/.exec(v);
        if (!m) return false;
        let s = m[1].split(' round ')[0].trim().split(/\s+/);
        s = s.length === 1 ? [s[0], s[0], s[0], s[0]] : s.length === 2 ? [s[0], s[1], s[0], s[1]]
            : s.length === 3 ? [s[0], s[1], s[2], s[1]] : s.slice(0, 4);
        if (s.some((x) => /px$/.test(x) && parseFloat(x) >= 999)) return true;
        const pct = (x) => (parseFloat(x) === 0 ? 0 : /%$/.test(x) ? parseFloat(x) : NaN);
        return pct(s[0]) + pct(s[2]) >= 100 || pct(s[1]) + pct(s[3]) >= 100;
    };
    const flat = (cs) => {
        const m = /^matrix\(([^)]*)\)$/.exec(cs.transform || '');
        if (m) {
            const [a, b, c, d] = m[1].split(',').map(parseFloat);
            if (a * d - b * c === 0) return true;
        }
        if (!cs.scale || cs.scale === 'none') return false;
        const s = cs.scale.split(/\s+/).map(parseFloat);
        return s[0] === 0 || s[s.length > 1 ? 1 : 0] === 0;
    };
    // Does this element hide everything inside it? Cached, and resolved
    // top-down without recursion, so a deeply nested page cannot overflow.
    const ruled = new Map();
    const hides = (el) => {
        const chain = [];
        for (let e = el; e && e !== document.documentElement && !ruled.has(e); e = e.parentElement)
            chain.push(e);
        for (let i = chain.length - 1; i >= 0; i--) {
            const e = chain[i];
            let h = !!(e.parentElement && ruled.get(e.parentElement));
            if (!h) {
                const cs = css(e);
                h = faint(e, cs) || clipRect(cs) || clipPath(cs.clipPath) || flat(cs);
                if (!h) {
                    const cx = /hidden|clip/.test(cs.overflowX), cy = /hidden|clip/.test(cs.overflowY);
                    if (cx || cy) {
                        const r = e.getBoundingClientRect();
                        h = (cx && r.width <= 1) || (cy && r.height <= 1);
                    }
                }
            }
            ruled.set(e, h);
        }
        return !!ruled.get(el);
    };
    // The outermost element whose hiding hides `el` (one hiding place).
    const hidingRoot = (el) => {
        let g = el;
        while (g.parentElement && ruled.get(g.parentElement)) g = g.parentElement;
        return g;
    };
    // Is rect r, inside element e, out of every reader's reach? Off the top or
    // the reachable left of the page it is, unless it lies inside the scroll
    // range of a scroll container that is itself in reach. The range starts at
    // the scroller's content edge (for RTL, scrollWidth - clientWidth further
    // left), so content pushed left of it is still out of reach.
    const offPage = (r, e) => {
        if (r.right + sx > minX && r.bottom + sy > 0) return false;
        for (let a = e; a && a !== document.body && a !== document.documentElement; a = a.parentElement) {
            const cs = css(a);
            if (!/auto|scroll/.test(cs.overflowX) && !/auto|scroll/.test(cs.overflowY)) continue;
            const b = a.getBoundingClientRect();
            const x0 = b.left + a.clientLeft - a.scrollLeft
                - (cs.direction === 'rtl' ? a.scrollWidth - a.clientWidth : 0);
            const y0 = b.top + a.clientTop - a.scrollTop;
            if (r.right <= x0 + 1 || r.bottom <= y0 + 1) return true;
            return offPage(b, a.parentElement);
        }
        return true;
    };
    const range = document.createRange();
    const textHidden = (n, p, cs) => {
        if (parseFloat(cs.fontSize) < 2 || hides(p)) return true;
        range.selectNodeContents(n);
        const r = range.getBoundingClientRect();
        return r.width < 1 || r.height < 1 || offPage(r, p);
    };
"""

