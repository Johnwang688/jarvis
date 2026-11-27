"""Fixed descriptions and actions in the isolated world of the last page read."""

from .read import _VISIBILITY
from .signin import Script


_HELPERS = _VISIBILITY + r"""
    const ref = refs[number - 1];
    const element = ref?.node;
    const formDescriptor = name => Object.getOwnPropertyDescriptor(HTMLFormElement.prototype, name).get;
    const input = node => node instanceof HTMLInputElement;
    const button = node => node instanceof HTMLButtonElement;
    const isSubmit = node => (button(node) || input(node)) && node.type === 'submit' ||
        input(node) && node.type === 'image';
    const fieldTypes = /^(text|search|email|url|tel|number)$/;
    const isField = node => node instanceof HTMLTextAreaElement ||
        input(node) && (fieldTypes.test(node.type) || node.type === 'password');
    const autocomplete = node => node.getAttribute('autocomplete') || '';
    const passwordField = node => node.matches('input, textarea, select') &&
        (node.type === 'password' || /current-password|new-password/i.test(autocomplete(node)));
    const paymentField = node => {
        if (/(?:^|\s)cc-/i.test(autocomplete(node)) ||
            /card|cvv|cvc|iban/i.test((node.getAttribute('name') || '') + ' ' + node.id)) return true;
        if (!node.matches('input, textarea, select')) return false;
        const hints = [node.getAttribute('placeholder'), node.getAttribute('aria-label'),
            ...Array.from(node.labels || [], label => label.textContent)]
            .join(' ').replace(/[-_\s]+/g, ' ');
        return /\b(card number|cvv|cvc|security code|iban)\b/i.test(hints) ||
            /\b(expiry|expiration)\b/i.test(hints) && /\b(card|credit|debit)\b/i.test(hints);
    };
    const sensitive = node => node.readOnly || node.type === 'password' || paymentField(node) ||
        /one-time-code|current-password|new-password/i.test(autocomplete(node)) ||
        /pass|pwd|otp|verification|cvv|cvc|card|iban|ssn|secret|token/i.test(
            [node.name, node.id, autocomplete(node)].join(' ')
        );

    function describe() {
        const image = ref?.imageMapHost;
        const visible = image ?
            usable(image) && element.isConnected && mapArea(element, imageMap(image)) : usable(element);
        if (!visible || document.designMode === 'on') return {kind: 'unusable'};
        const targetRef = refs[ref.targetNumber - 1];
        const node = activationTarget(element);
        if (!targetRef || node !== targetRef.node || !matchesMetadata(ref) ||
            (node !== element && (!usable(node) || !matchesMetadata(targetRef)))) {
            return {kind: 'unusable'};
        }
        const link = node instanceof HTMLAnchorElement || node instanceof HTMLAreaElement;
        const form = actionForm(node);
        const buttonLike = button(node) || input(node) && /^(button|reset|submit|image)$/.test(node.type) ||
            node.getAttribute('role') === 'button';
        let kind;
        if (link) {
            kind = node.hasAttribute('href') && /^https?:\/\//.test(node.href) ? 'link' : 'unusable';
        } else if (form && isSubmit(node)) {
            kind = 'submit';
        } else if (input(node) && /^(checkbox|radio)$/.test(node.type)) {
            kind = 'toggle';
        } else if (isField(node)) {
            kind = 'field';
        } else if (!targetRef.editable && node instanceof HTMLElement &&
            (button(node) || input(node) && /^(button|reset)$/.test(node.type) ||
             node.localName === 'summary' || /^(button|tab|menuitem)$/.test(node.getAttribute('role')))) {
            kind = 'control';
        } else {
            kind = 'unusable';
        }
        if (kind === 'unusable') return {kind};

        const fields = form ? Array.from(formDescriptor('elements').call(form)) : [];
        let submitter = kind === 'submit' ? node : null;
        if (kind === 'field' && form) {
            const controls = document.querySelectorAll('button, input[type=submit i], input[type=image i]');
            const eligible = Array.from(controls).filter(control =>
                control.form === form && isSubmit(control) && usable(control));
            submitter = eligible.find(control =>
                node.compareDocumentPosition(control) & Node.DOCUMENT_POSITION_FOLLOWING
            ) || eligible[0] || null;
        }
        const address = form ? (submitter?.hasAttribute('formaction') ? submitter.formAction :
            formDescriptor('action').call(form)) : '';
        const method = form ? (submitter?.hasAttribute('formmethod') ? submitter.formMethod :
            formDescriptor('method').call(form)) : '';
        const target = form ? ((submitter?.getAttribute('formtarget') ?? formDescriptor('target').call(form)) ||
            document.querySelector('base[target]')?.getAttribute('target') || '') : '';
        let destination = '';
        try {
            const url = new URL(address);
            if (/^https?:$/.test(url.protocol)) destination = url.origin;
        } catch {}
        const publicInfo = {
            kind, button_like: buttonLike, target_number: ref.targetNumber,
            form_has_password: fields.some(passwordField),
            payment: paymentField(node) || fields.some(paymentField),
            destination_origin: destination, method, sensitive: kind === 'field' && sensitive(node),
            textarea: node instanceof HTMLTextAreaElement, can_submit: Boolean(form),
        };
        // Keep the unredacted destination and the DOM objects inside this world.
        const signature = JSON.stringify([publicInfo, node.type, address, target,
            submitter?.name, submitter?.value, submitter?.formNoValidate, submitter?.formEnctype,
            form && formDescriptor('enctype').call(form), form && formDescriptor('noValidate').call(form)]);
        return {publicInfo, signature, form, submitter, node};
    }

    function same(left, right) {
        return left?.signature === right.signature && left?.form === right.form &&
            left?.submitter === right.submitter && left?.node === right.node;
    }

    function submit({publicInfo, form, submitter, node}) {
        if (!form || publicInfo.form_has_password ||
            (!publicInfo.destination_origin && publicInfo.method !== 'dialog')) return 'changed';
        if (submitter) HTMLFormElement.prototype.requestSubmit.call(form, submitter);
        else node.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', code: 'Enter', bubbles: true}));
        return 'done';
    }
"""


_DESCRIBE = r"""
    const current = describe();
    if (!current.signature) return {kind: 'unusable'};
    const previous = bindings.get(number);
    current.binding = same(previous, current) ? previous.binding : (previous?.binding || 0) + 1;
    bindings.set(number, current);
    return {...current.publicInfo, binding: current.binding};
"""


_CLICK = r"""
    const current = describe();
    const approved = bindings.get(number);
    if (!current.signature) return 'changed';
    if (approved?.binding !== binding || !same(approved, current)) return 'changed';
    if (current.publicInfo.kind === 'submit') return submit(current);
    if (!['toggle', 'control'].includes(current.publicInfo.kind)) return 'changed';
    current.node.click();
    return 'done';
"""


_TYPE = r"""
    const current = describe();
    const approved = bindings.get(number);
    if (!current.signature) return 'changed';
    if (approved?.binding !== binding || !same(approved, current)) return 'changed';
    const {publicInfo, node} = current;
    if (publicInfo.kind !== 'field' || publicInfo.sensitive ||
        (send && (!publicInfo.can_submit || publicInfo.form_has_password ||
                  (!publicInfo.destination_origin && publicInfo.method !== 'dialog')))) {
        return 'changed';
    }
    if (typeof text !== 'string' || !text || (!publicInfo.textarea && text.includes('\n'))) return 'changed';
    const setter = Object.getOwnPropertyDescriptor(publicInfo.textarea ?
        HTMLTextAreaElement.prototype : HTMLInputElement.prototype, 'value').set;
    const probe = document.createElement(node.localName);
    if (!publicInfo.textarea) {
        probe.type = node.type;
        probe.multiple = node.multiple;
    }
    setter.call(probe, text);
    if (probe.value !== text) return 'invalid_text';

    node.focus({preventScroll: true});
    if (!same(current, describe())) return 'changed';
    setter.call(node, text);
    node.dispatchEvent(new Event('input', {bubbles: true}));
    node.dispatchEvent(new Event('change', {bubbles: true}));
    if (!send) return 'done';
    const after = describe();
    if (!same(current, after)) return 'filled';
    return submit(after);
"""


DESCRIBE = Script(_DESCRIBE)
CLICK = Script(_CLICK)
TYPE = Script(_TYPE)


def document_function(script):
    if script is not DESCRIBE and script is not CLICK and script is not TYPE:
        raise TypeError("Only the built-in browser action scripts are allowed.")
    return """function (origin, worldKey, number, binding, text, send) {
    if (window.top !== window || location.origin !== origin || self.origin !== origin ||
        document.domain !== location.hostname || typeof worldKey !== 'string' ||
        globalThis.__jarvisReadKey !== worldKey) return 'moved';
    const refs = globalThis.__jarvisReadRefs, bindings = globalThis.__jarvisActBindings;
    if (!Array.isArray(refs) || !(bindings instanceof Map)) return 'moved';
    if (!Number.isSafeInteger(number) || number < 1) return {kind: 'unusable'};
""" + _HELPERS + script.source + "\n}"
