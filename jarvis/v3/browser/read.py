"""Read rendered main-document text as untrusted data."""

from typing import NamedTuple

from jarvis import untrusted
from jarvis.v2 import redact
from jarvis.v3.browser.session import BrowserError, Location, _READ_MOVED, _read_origin
from jarvis.v3.browser.signin import Script


READ = Script(r"""function () {
    const nodeCap = 20_000, byteCap = 256 * 1024, refCap = 200;
    const omitted = new Set([
        'head', 'script', 'style', 'noscript', 'template', 'iframe', 'frame', 'noframes',
        'input', 'textarea', 'select', 'option', 'datalist',
    ]);
    const roles = new Map([
        ['button', 'button'], ['link', 'link'], ['checkbox', 'checkbox'],
        ['tab', 'other'], ['menuitem', 'other'],
    ]);
    const encoder = new TextEncoder();
    const range = document.createRange();
    const root = document.documentElement;
    const scroller = document.scrollingElement || root;
    const rootStyle = getComputedStyle(root);
    const rtl = rootStyle.direction === 'rtl';
    const bodyOverflowIsViewport = rootStyle.overflowX === 'visible' &&
        rootStyle.overflowY === 'visible';
    const page = {
        left: (rtl ? scroller.clientWidth - scroller.scrollWidth : 0) - scrollX,
        right: (rtl ? scroller.clientWidth : scroller.scrollWidth) - scrollX,
        top: -scrollY,
        bottom: scroller.scrollHeight - scrollY,
    };
    const viewport = {left: 0, right: scroller.clientWidth, top: 0, bottom: scroller.clientHeight};
    const designMode = document.designMode === 'on';

    function intersects(box, area) {
        return box.width > 0 && box.height > 0 &&
            box.right > area.left && box.left < area.right &&
            box.bottom > area.top && box.top < area.bottom;
    }

    function clipped(style, box) {
        const rect = /^rect\((.*)\)$/.exec(style.clip);
        if (rect && /^(absolute|fixed)$/.test(style.position)) {
            const [top, right, bottom, left] = rect[1].split(/[,\s]+/).map(parseFloat);
            if (right <= left || bottom <= top) return true;
        }
        const inset = /^inset\(([-\d.%px\s]+)(?: round .*)?\)$/.exec(style.clipPath);
        if (!inset) return false;
        const [top, right = top, bottom = top, left = right] = inset[1].trim().split(/\s+/);
        const distance = (part, size) =>
            parseFloat(part) * (part.endsWith('%') ? size / 100 : 1);
        return distance(top, box.height) + distance(bottom, box.height) >= box.height ||
            distance(left, box.width) + distance(right, box.width) >= box.width;
    }

    function tinyClip(overflow, size, clientSize) {
        return /^(hidden|clip)$/.test(overflow) && Math.min(size, clientSize) <= 1;
    }

    function contentArea(element, style, box, outer) {
        if (element === scroller || (element === document.body && bodyOverflowIsViewport) ||
            (style.overflowX === 'visible' && style.overflowY === 'visible')) return outer;

        function axis(overflow, edge, client, scroll, position, reversed, low, high) {
            if (overflow === 'auto' || overflow === 'scroll') {
                const start = edge - position - (reversed ? scroll - client : 0);
                return [start, start + scroll];
            }
            if (overflow === 'hidden' || overflow === 'clip') {
                return [Math.max(edge, low), Math.min(edge + client, high)];
            }
            return [low, high];
        }

        const [left, right] = axis(style.overflowX, box.left + element.clientLeft,
            element.clientWidth, element.scrollWidth, element.scrollLeft,
            style.direction === 'rtl', outer.left, outer.right);
        const [top, bottom] = axis(style.overflowY, box.top + element.clientTop,
            element.clientHeight, element.scrollHeight, element.scrollTop,
            false, outer.top, outer.bottom);
        return {left, right, top, bottom};
    }

    function kindOf(element, tag) {
        if (tag === 'input') {
            if (element.type === 'hidden') return;
            if (element.type === 'checkbox') return 'checkbox';
            if (/^(button|submit|reset|image)$/.test(element.type)) return 'button';
            return 'field';
        }
        if (tag === 'textarea') return 'field';
        if (tag === 'select') return 'select';
        if (tag === 'button') return 'button';
        if (tag === 'a' && element.hasAttribute('href')) return 'link';
        return roles.get(element.getAttribute('role'));
    }

    const parts = [], controls = [], others = [], names = new Map();
    let visits = 0, bytes = 0, length = 0, pendingBreak = false, more = false;
    let stopped = false;
    function append(value, collapse) {
        if (collapse) {
            value = value.replace(/[\t\n\r\f ]+/g, ' ');
            if (value.startsWith(' ') &&
                (pendingBreak || parts[parts.length - 1]?.endsWith(' '))) value = value.slice(1);
        } else {
            // Match Python's cleaned line boundaries before recording label offsets.
            value = value.replace(/\r\n|[\r\v\f\x85\u2028\u2029]/g, '\n');
        }
        if (!value) return;
        const separator = pendingBreak && length ? '\n' : '';
        if (value.length + separator.length > byteCap - bytes) {
            stopped = true;
            return;
        }
        const size = encoder.encode(value).length + separator.length;
        if (size > byteCap - bytes) {
            stopped = true;
            return;
        }
        parts.push(separator + value);
        length += separator.length + value.length;
        bytes += size;
        pendingBreak = false;
    }

    // Iterative in document order, keeping only the current ancestry on the stack.
    const stack = [{next: root, area: page, style: null, ariaDisabled: false}];
    while (stack.length && !stopped) {
        const parent = stack[stack.length - 1];
        const node = parent.next;
        if (!node) {
            if (parent.name) parent.name.end = length;
            if (parent.block) pendingBreak = true;
            stack.pop();
            continue;
        }
        if (++visits > nodeCap) {
            stopped = true;
            break;
        }
        parent.next = node.nextSibling;
        // In design mode the body is the editor. Ignore stray document children too.
        if (designMode && node !== root && node !== document.body) continue;
        if (node.nodeType === Node.TEXT_NODE) {
            const style = parent.style;
            if (!style || style.visibility !== 'visible' || parseFloat(style.fontSize) < 4) continue;
            range.selectNodeContents(node);
            if (intersects(range.getBoundingClientRect(), parent.area)) {
                const value = node.data;
                // Bound work on a single text node before whitespace normalization.
                if (value.length > byteCap) {
                    stopped = true;
                    break;
                }
                append(value, style.whiteSpaceCollapse === 'collapse');
            }
            continue;
        }
        if (node.nodeType !== Node.ELEMENT_NODE) continue;
        const tag = node.localName;
        const control = tag === 'input' || tag === 'textarea' || tag === 'select';
        if ((omitted.has(tag) && !control) ||
            node.matches('[hidden], [inert], [aria-hidden="true" i], details:not([open])')) continue;

        const style = getComputedStyle(node);
        if (style.display === 'none' || style.contentVisibility === 'hidden' ||
            Number(style.opacity) === 0) continue;
        const box = node.getBoundingClientRect();
        const hasBox = style.display !== 'contents';
        if (tag === 'br' && style.visibility === 'visible' && box.height > 0 &&
            box.left >= parent.area.left && box.left < parent.area.right &&
            box.bottom > parent.area.top && box.top < parent.area.bottom) {
            pendingBreak = true;
        }
        // A border can make the bounding box large while the clip remains tiny.
        const clipWidth = style.display === 'inline' ? box.width : node.clientWidth;
        const clipHeight = style.display === 'inline' ? box.height : node.clientHeight;
        if (hasBox && (!intersects(box, parent.area) || clipped(style, box) ||
            (style.position === 'fixed' && !intersects(box, viewport)) ||
            tinyClip(style.overflowX, box.width, clipWidth) ||
            tinyClip(style.overflowY, box.height, clipHeight))) continue;

        const block = hasBox && !/^(inline|ruby)/.test(style.display);
        if (block) pendingBreak = true;
        const editable = (designMode && node === document.body) ||
            node.matches('[contenteditable]:not([contenteditable="false" i])') ||
            // A role attribute is a token list: " textbox " and "nonesuch textbox" are textboxes too.
            (node.getAttribute('role') || '').toLowerCase().split(/\s+/).includes('textbox');
        const kind = control ? kindOf(node, tag) : editable ? 'field' : kindOf(node, tag);
        const ariaDisabled = parent.ariaDisabled || node.matches('[aria-disabled="true" i]');
        const visible = hasBox && style.visibility === 'visible' && parseFloat(style.fontSize) >= 4;
        if (tag === 'img' && visible && parent.imageCaption && !parent.imageCaption.image) {
            const alt = attribute(node, 'alt');
            if (alt) parent.imageCaption.image = alt;
        }
        let name;
        let imageCaption = parent.imageCaption;
        if (kind && visible && !ariaDisabled && !node.matches(':disabled')) {
            const group = kind === 'link' || kind === 'other' ? others : controls;
            if (group.length < refCap) {
                name = {start: length, end: length};
                const candidate = {node, kind, name, editable, order: visits};
                group.push(candidate);
                if (kind === 'link' || kind === 'button') imageCaption = candidate;
            } else {
                more = true;
            }
        }
        if (node instanceof HTMLLabelElement) {
            name ||= {start: length, end: length};
            names.set(node, name);
        }
        if (!control && !editable && node.firstChild) {
            stack.push({
                next: node.firstChild, style, ariaDisabled, name, block, imageCaption,
                area: hasBox ? contentArea(node, style, box, parent.area) : parent.area,
            });
        }
    }

    let text = parts.join('');
    if (stopped) {
        // The last line may be incomplete. Do not hand a partial credential to Python.
        if (!pendingBreak) text = text.slice(0, text.lastIndexOf('\n') + 1);
    }
    const candidates = controls.concat(others.slice(0, refCap - controls.length))
        .sort((a, b) => a.order - b.order);
    more ||= candidates.length < controls.length + others.length;
    const visibleName = span => span ? text.slice(span.start, span.end).trim() : '';

    // Resolve only visited labels, with an explicit association for custom editors.
    const selected = new Map(candidates.map(candidate => [candidate.node, candidate]));
    const associated = new Map();
    for (const [label, span] of names) {
        const target = label.control || (label.htmlFor && document.getElementById(label.htmlFor));
        if (selected.has(target) && (label.control || selected.get(target).editable) &&
            !associated.has(target) && visibleName(span)) {
            associated.set(target, span);
        }
    }

    function bounded(value) {
        if (!value) return '';
        if (value.length > byteCap - bytes) {
            more = true;
            return '';
        }
        const size = encoder.encode(value).length;
        if (size > byteCap - bytes) {
            more = true;
            return '';
        }
        bytes += size;
        return value;
    }

    const title = bounded(document.title);
    function attribute(node, key) {
        const value = node.getAttribute(key) || '';
        // Let bounded() refuse an oversized attribute in its entirety.
        return value.length > byteCap ? value : value.trim();
    }
    const lineStarts = [0];
    for (let i = text.indexOf('\n'); i !== -1; i = text.indexOf('\n', i + 1)) {
        lineStarts.push(i + 1);
    }
    function lineAt(offset) {
        let low = 0, high = lineStarts.length;
        while (low + 1 < high) {
            const middle = (low + high) >> 1;
            if (lineStarts[middle] <= offset) low = middle;
            else high = middle;
        }
        return low;
    }
    function labelOf({node, kind, name, editable, image}) {
        const item = (value, labelLines = []) => ({kind, label: bounded(value), labelLines});
        const fromText = span => {
            if (!span) return;
            const raw = text.slice(span.start, span.end);
            const value = raw.trim();
            if (!value) return;
            const label = bounded(value);
            if (!label) return {kind, label, labelLines: []};
            const start = span.start + raw.length - raw.trimStart().length;
            const first = lineAt(start), last = lineAt(start + value.length - 1);
            return {kind, label, labelLines: Array.from({length: last - first + 1}, (_, i) => first + i)};
        };

        const input = node.localName === 'input';
        if (input && node.type === 'password') return {kind, label: 'password field', labelLines: []};
        const aria = attribute(node, 'aria-label');
        if (aria) return item(aria);
        const wrapper = editable ? node.closest('label:not([for])') : null;
        const label = fromText(associated.get(node) || (wrapper && !wrapper.control && names.get(wrapper)));
        if (label) return label;
        // Without a value attribute the browser shows its own caption on these two.
        const caption = input && /^(submit|button|reset)$/.test(node.type) && (attribute(node, 'value') ||
            (!node.hasAttribute('value') && {submit: 'Submit', reset: 'Reset'}[node.type]));
        const placeholder = attribute(node, 'placeholder') || (editable && attribute(node, 'aria-placeholder'));
        if (caption || placeholder) return item(caption || placeholder);
        const title = attribute(node, 'title');
        if (editable) return item(title);
        return fromText(name) || item(image || title || attribute(node, 'alt') || attribute(node, 'name'));
    }
    const items = candidates.map(labelOf);
    // This isolated world lives only as long as the document. Index n-1 is ref n.
    globalThis.__jarvisReadRefs = candidates.map(({node}) => node);
    return {title, text, items, more: more || stopped};
}""")


def document_function(script: Script) -> str:
    if script is not READ:
        raise TypeError("Only the built-in read script is allowed.")
    return """function (origin) {
    if (window.top !== window || location.origin !== origin) return "moved";
    return (""" + script.source + """)();
}"""


class Ref(NamedTuple):
    number: int
    kind: str
    label: str


class PageView(NamedTuple):
    location: Location
    title: str
    text: str
    refs: tuple[Ref, ...]
    truncated: bool


_WITHHELD = "[a line was withheld]"
_KINDS = frozenset({"link", "button", "field", "select", "checkbox", "other"})


def _withhold_lines(lines: list[str]) -> set[int]:
    """Screen in place, preserving the script's line indexes."""
    cleaned = "\n".join(lines)
    if redact.for_display(cleaned) == cleaned:
        return set()

    # Screen the whole field first so a shape split over lines is withheld
    # on every line it touches.
    spans = redact.spans(cleaned, mode=redact.DISPLAY)
    withheld = set()
    offset = index = 0
    for number, line in enumerate(lines):
        end = offset + len(line)
        while index < len(spans) and spans[index][1] <= offset:
            index += 1
        if (index < len(spans) and spans[index][0] < end) or redact.labelled_spans(line):
            lines[number] = _WITHHELD
            withheld.add(number)
        offset = end + 1

    cleaned = "\n".join(lines)
    if redact.for_display(cleaned) != cleaned:
        for number, line in enumerate(lines):
            if redact.for_display(line) != line:
                lines[number] = _WITHHELD
                withheld.add(number)
        cleaned = "\n".join(lines)
        # The known-value scrub may also recognize a value across lines.
        if redact.for_display(cleaned) != cleaned:
            withheld.update(range(len(lines)))
            lines[:] = [_WITHHELD]
    return withheld


def _join_lines(lines: list[str]) -> str:
    compact = []
    for line in lines:
        line = line.strip()
        if line or (compact and compact[-1]):
            compact.append(line)
    return "\n".join(compact).rstrip()


def _screen(value: str) -> str:
    lines = untrusted.strip_invisible(value).splitlines()
    _withhold_lines(lines)
    return _join_lines(lines)


def read_page(session) -> PageView:
    """Read the current page. Title and ref labels are untrusted data too.

    A model-facing caller must fence those fields before presenting them.
    """
    location = session.location()
    result = session.read_call(location)
    if result == "moved" or not session.is_current(location):
        raise BrowserError(_READ_MOVED, reason="moved")
    try:
        title, text, items, more = (result[key] for key in ("title", "text", "items", "more"))
        if (type(title) is not str or type(text) is not str or type(items) is not list or
                type(more) is not bool or len(items) > 200):
            raise ValueError
        lines = untrusted.strip_invisible(text).split("\n")
        line_count = len(lines)
        withheld = _withhold_lines(lines)
        refs = []
        for number, item in enumerate(items, 1):
            kind, label, sources = item["kind"], item["label"], item["labelLines"]
            if (kind not in _KINDS or type(label) is not str or type(sources) is not list or
                    any(type(source) is not int or not 0 <= source < line_count for source in sources)):
                raise ValueError
            label = "[withheld]" if withheld.intersection(sources) else _screen(label)[:80]
            refs.append(Ref(number, kind, label))
        body = untrusted.neutralize_markers(_join_lines(lines))
        truncated = more or len(body) > 4_000
        if len(body) > 4_000:
            body = body[:body.rfind("\n", 0, 4_001) + 1].rstrip()
        return PageView(location, _screen(title)[:120],
                        untrusted.fence(body, _read_origin(location)), tuple(refs), truncated)
    except Exception:
        # Conversion and redaction failures may carry page data.
        pass
    raise BrowserError("The browser page could not be read safely.", reason="unsettled")
