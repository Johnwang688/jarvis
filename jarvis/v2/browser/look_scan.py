"""Bounded isolated-world scan, without touching the owner's DOM (BR5)."""

from jarvis.browser_visibility import _JUDGE_JS

NODE_CAP = 20_000
RAW_CAP = 256 * 1024
REF_CAP = 200
SCAN_TIMEOUT_S = 5.0

# Assignments below touch only the isolated world's globals and Maps. There
# are no DOM writes, no main-world callbacks and no form-control value reads.
SCAN_JS = r"""(args) => {
    if (location.href !== args.url) return {changed:true};
""" + _JUDGE_JS + r"""
    const old = globalThis.__jarvisLook;
    if (args.replace && old && old.observer) old.observer.disconnect();
    const state = args.replace ? {refs:new Map(), serial:args.serial, changed:false} : old;
    if (!state || state.serial !== args.serial) return {changed:true};
    if (args.replace) {
        state.observer = new MutationObserver(() => { state.changed = true; });
        state.observer.observe(document, {subtree:true, childList:true, attributes:true, characterData:true});
        globalThis.__jarvisLook = state;
    }
    const out = {url:location.href, title:document.title, lines:[], refs:[], masks:[],
                 raw:[], width:innerWidth, height:innerHeight, serial:args.serial,
                 changed:state.changed, incomplete:false};
    let size = 0, visits = 0;
    const charge = (value) => {
        const text = String(value || ''); size += text.length;
        if (size > args.rawCap) { out.incomplete=true; return ''; }
        out.raw.push(text); return text;
    };
    charge(out.url); charge(out.title);
    const viewport = r => r.right > 0 && r.bottom > 0 && r.left < innerWidth && r.top < innerHeight;
    const boxes = e => {
        const list=e.getClientRects();
        if(list.length+out.masks.length>2000) {out.incomplete=true;return [];}
        return Array.from(list).map(r => [r.left,r.top,r.right,r.bottom]);
    };
    const masked = new Set(['INPUT','TEXTAREA','SELECT','IFRAME','FRAME']);
    const omitted = new Set(['SCRIPT','STYLE','TEMPLATE','NOSCRIPT','DATALIST','CANVAS','VIDEO','AUDIO','OBJECT']);
    const candidate = e => /^(A|BUTTON|INPUT|TEXTAREA|SELECT|SUMMARY)$/.test(e.tagName)
        || /^(button|link|checkbox|radio|tab|menuitem|switch|combobox|textbox)$/.test(e.getAttribute('role') || '');
    const candidates = [], names = new Map(), textParts = [];
    const stack = [document.documentElement];
    while (stack.length && !out.incomplete) {
        const n = stack.pop(); if (++visits > args.nodeCap) { out.incomplete=true; break; }
        if (n.nodeType === Node.TEXT_NODE) {
            const p=n.parentElement;
            if (!p || p.isContentEditable) continue;
            const cs=css(p);
            if (cs.display==='none' || cs.visibility!=='visible' || textHidden(n,p,cs)) continue;
            range.selectNodeContents(n); const r=range.getBoundingClientRect();
            if (!args.whole && !viewport(r)) continue;
            const value=charge(n.nodeValue); textParts.push(value);
            if (value.trim()) out.lines.push(value);
            for(let a=p;a;a=a.parentElement) {
                if(names.has(a)) { names.get(a).push(value); break; }
            }
            continue;
        }
        if (n.nodeType !== Node.ELEMENT_NODE) continue;
        const e=n, cs=css(e), r=e.getBoundingClientRect();
        const isMask=masked.has(e.tagName) || e.isContentEditable;
        if (isMask && viewport(r)) out.masks.push(...boxes(e));
        if (candidate(e) && cs.display!=='none' && cs.visibility==='visible'
                && !hides(e) && parseFloat(cs.opacity)>0.05 && r.width>=8 && r.height>=8
                && viewport(r) && !offPage(r,e)) {
            const x=r.left+r.width/2, y=r.top+r.height/2;
            const hit=document.elementFromPoint(x,y);
            if(x>=0 && x<innerWidth && y>=0 && y<innerHeight && hit && (hit===e || e.contains(hit))) {
                if(candidates.length>=args.refCap) {out.incomplete=true;break;}
                const attrs={};
                for(const key of ['role','type','aria-label','title','placeholder','href']) attrs[key]=charge(e.getAttribute(key));
                candidates.push({e,attrs}); names.set(e,[]);
            }
        }
        // Editable and form-control children may be their current/default value.
        if (isMask || omitted.has(e.tagName)) continue;
        const shadow=e.shadowRoot;
        if(e.childNodes.length+(shadow?shadow.childNodes.length:0)>args.nodeCap-visits) {out.incomplete=true;break;}
        const children=Array.from(e.childNodes);
        if(shadow) children.push(...shadow.childNodes);
        for(let i=children.length-1;i>=0;i--) stack.push(children[i]);
    }
    // Adjacent text nodes can split a recognizable credential. Guard their
    // joined spelling too, even though presentation keeps readable lines.
    charge(textParts.join(''));
    if(out.incomplete) {state.refs.clear();return {incomplete:true};}
    for(let i=0;i<candidates.length;i++) {
        const {e,attrs}=candidates[i], ref='e'+(i+1);
        const visible=names.get(e).join(' ').trim();
        const label=visible || attrs['aria-label'] || attrs.title || attrs.placeholder || '';
        out.refs.push({ref,tag:/^(A|BUTTON|INPUT|TEXTAREA|SELECT|SUMMARY)$/.test(e.tagName)?e.tagName.toLowerCase():'element',label,attrs});
        if(args.replace) state.refs.set(ref,e);
    }
    out.changed = state.changed;
    return out;
}"""

CLEAR_JS = "globalThis.__jarvisLook?.observer?.disconnect(); globalThis.__jarvisLook?.refs?.clear(); delete globalThis.__jarvisLook;"
