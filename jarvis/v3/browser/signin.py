"""Owner-initiated sign-in using fixed scripts and an exact saved origin."""

from dataclasses import dataclass
import time
from typing import NamedTuple

from jarvis.v3.logins import LoginError, canonical_origin


@dataclass(frozen=True)
class Script:
    source: str


# Both scripts use the same field selection and document signature. The action
# selects again in its own document turn, before it touches a field.
_DOCUMENT = r"""
    function visible(element) {
        const box = element.getBoundingClientRect();
        if (!box.width || !box.height) return false;
        for (let node = element; node; node = node.parentElement) {
            if (node.matches('[inert], [aria-hidden="true" i], details:not([open])')) return false;
            const style = getComputedStyle(node);
            if (style.display === 'none' || (node === element && style.visibility !== 'visible') ||
                Number(style.opacity) === 0) return false;
        }
        return true;
    }

    function editable(field) {
        return field.isConnected && !field.matches(':disabled') && !field.readOnly && visible(field);
    }

    // Only numeric counts accompany the fixed result string. Text stays in the document.
    function signature(text = document.body?.innerText || '') {
        const messages = document.querySelectorAll('[role=alert], [aria-invalid="true"], [aria-live]');
        let count = 0;
        for (const message of messages) {
            if (visible(message) && (message.innerText ?? message.textContent).trim()) count++;
        }
        return [text.length, count];
    }

    function userRank(field) {
        if (!/^(text|email|tel)$/.test(field.type)) return 0;
        if (/\b(username|email)\b/i.test(field.autocomplete)) return 3;
        if (field.type === 'email') return 2;
        if (/^(text|tel)$/.test(field.type) && /user|login|email|identifier|account/i.test(
            [field.name, field.id, field.autocomplete].join(' ')
        )) return 1;
        return 0;
    }

    function fields() {
        const inputs = Array.from(document.querySelectorAll('input')).filter(field =>
            /^(text|tel|email|search|url|number|password)$/.test(field.type) && editable(field)
        );
        const isCode = field => /\bone-time-code\b/i.test(field.autocomplete);
        if (inputs.some(isCode)) return {kind: 'code'};
        const users = inputs.filter(userRank);
        const passwords = inputs.filter(field => field.type === 'password');
        function bestUser(field) {
            let best;
            for (const user of users) {
                if (!field || (user.form === field.form &&
                    (user.compareDocumentPosition(field) & Node.DOCUMENT_POSITION_FOLLOWING))) {
                    if (!best || userRank(user) > userRank(best) ||
                        (field && userRank(user) === userRank(best))) best = user;
                }
            }
            return best;
        }
        const password = passwords.find(field => field.form && bestUser(field)) ||
            passwords.find(field => field.form) ||
            passwords.find(field => bestUser(field)) || passwords[0];
        const username = bestUser(password);
        const kind = password ? (username ? 'both' : 'password') : (username ? 'username' : 'none');
        return {kind, password, username};
    }
"""

INSPECT = Script(r"""function (_origin, readText) {
""" + _DOCUMENT + r"""
    let {kind} = fields();
    const text = document.body?.innerText || '';
    if (kind === 'none' && readText === 'after' &&
        /verification\s+code|two-step|2-step|two-factor|authenticator|enter\s+the\s+code|security\s+code/i
            .test(text)) kind = 'code';
    return {result: kind, signature: signature(text)};
}""")

ACT = Script("""function (origin, expected, username, secret) {
""" + _DOCUMENT + r"""
    const selected = fields();
    if (selected.kind !== expected || !['both', 'username', 'password'].includes(expected)) {
        return 'failed';
    }
    const {username: userField, password: passwordField} = selected;
    const field = passwordField || userField;
    const form = field.form;
    const userForm = userField?.form;
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;

    function sameField(input, owner, types) {
        return input instanceof HTMLInputElement && input.ownerDocument === document &&
            input.isConnected && input.form === owner && types.includes(input.type);
    }

    function stillEditable(input, owner, types) {
        return sameField(input, owner, types) && editable(input);
    }

    function setValue(field, value) {
        setter.call(field, value);
        field.dispatchEvent(new Event('input', {bubbles: true}));
        field.dispatchEvent(new Event('change', {bubbles: true}));
    }

    const isSubmit = control => control.type === 'submit' ||
        (control instanceof HTMLInputElement && control.type === 'image');
    function controls() {
        return Array.from(document.querySelectorAll('button, input[type=submit i], input[type=image i]'));
    }

    // Read the native properties even when named controls shadow them on the form.
    const action = Object.getOwnPropertyDescriptor(HTMLFormElement.prototype, 'action').get;
    const method = Object.getOwnPropertyDescriptor(HTMLFormElement.prototype, 'method').get;
    const formTarget = Object.getOwnPropertyDescriptor(HTMLFormElement.prototype, 'target').get;
    function nativeOK(submitter) {
        const address = submitter?.hasAttribute('formaction') ? submitter.formAction : action.call(form);
        const verb = submitter?.hasAttribute('formmethod') ? submitter.formMethod : method.call(form);
        // An empty target still inherits the document's base target, if any.
        // Only an HTML base element counts (an element named base in another
        // namespace does not set the target).
        const base = Array.from(document.querySelectorAll('base[target]'))
            .find((element) => element instanceof HTMLBaseElement);
        const target = (submitter?.getAttribute('formtarget') ?? formTarget.call(form)) ||
            base?.getAttribute('target') || '';
        try {
            const destination = new URL(address || location.href);
            return verb === 'post' && destination.protocol === 'https:' && destination.origin === origin &&
                (!target || target.toLowerCase() === '_self');
        } catch {
            return false;
        }
    }

    function submission() {
        if (!form) return {};
        const associated = controls().filter(control => control.form === form && isSubmit(control));
        const eligible = associated.filter(control => !control.matches(':disabled') && visible(control));
        const submitter = eligible.find(control =>
            field.compareDocumentPosition(control) & Node.DOCUMENT_POSITION_FOLLOWING
        ) || eligible[0];
        if (!submitter && associated.length) return {refusal: 'failed'};
        return {submitter, nativeOK: nativeOK(submitter)};
    }

    const initial = submission();
    if (passwordField && initial.refusal) return initial.refusal;
    const userTypes = ['text', 'email', 'tel'];
    const passwordTypes = ['password'];
    if (userField) {
        userField.focus();
        if (!stillEditable(userField, userForm, userTypes)) return 'moved';
        setValue(userField, username);
    }
    if (passwordField) {
        if (!stillEditable(passwordField, form, passwordTypes)) return 'moved';
        passwordField.focus();
        const focused = submission();
        if (!stillEditable(passwordField, form, passwordTypes)) return 'moved';
        if (focused.refusal) return focused.refusal;
        // A previously safe destination can change during the fill's focus events.
        if (initial.nativeOK && !focused.nativeOK) return 'moved';
        setValue(passwordField, secret);
    }

    const {submitter, refusal, nativeOK: allowNative} = submission();
    const current = sameField(field, form, passwordField ? passwordTypes : userTypes);
    if (!current || refusal) {
        if (passwordField) setValue(passwordField, '');
        return current ? refusal : 'moved';
    }
    if (form) {
        const requestSubmit = HTMLFormElement.prototype.requestSubmit;
        function cancelNative(event) {
            if (event.target === form) event.preventDefault();
        }
        // Window capture runs before document and form listeners. Keep the
        // guard only for this synchronous requestSubmit, even if validation fails.
        const before = signature();
        if (!allowNative) window.addEventListener('submit', cancelNative, true);
        try {
            if (submitter) requestSubmit.call(form, submitter);
            else requestSubmit.call(form);
        } finally {
            if (!allowNative) window.removeEventListener('submit', cancelNative, true);
        }
        return {result: allowNative ? 'submitted' : 'handled', before};
    }

    const buttons = controls().filter(
        button => !button.form && !button.matches(':disabled') &&
            visible(button) && (isSubmit(button) || /next|continue|sign\s+in|log\s+in|submit/i.test(button.innerText))
    );
    // Compare distance through the DOM to the first common ancestor.
    const ancestors = [];
    for (let node = field; node; node = node.parentElement) ancestors.push(node);
    function distance(button) {
        let steps = 0;
        while (!ancestors.includes(button)) {
            button = button.parentElement;
            steps++;
        }
        return steps + ancestors.indexOf(button);
    }
    buttons.sort((left, right) => distance(left) - distance(right));
    const before = signature();
    if (buttons.length) {
        buttons[0].click();
    } else {
        field.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', code: 'Enter', bubbles: true}));
    }
    return {result: 'submitted', before};
}""")


def document_function(script):
    if script is not INSPECT and script is not ACT:
        raise TypeError("Only a built-in sign-in script is allowed.")
    return """function (origin, ...args) {
    if (window.top !== window || location.protocol !== "https:" ||
        location.origin !== origin || self.origin !== origin ||
        document.domain !== location.hostname) return "moved";
    return (""" + script.source + """)(origin, ...args);
}"""


class Outcome(NamedTuple):
    result: str
    detail: str


_DETAILS = {
    "signed_in": "The site accepted the form.",
    "needs_code": "The site is asking for a verification code.",
    "rejected": "The site is still asking for a password.",
    "no_form": "No sign-in form was found.",
    "moved": "The browser moved away from the sign-in document.",
    "wrong_site": "The page is outside the saved site's origin.",
    "wrong_destination": (
        "The form on that page would have sent its contents to another site or put them "
        "in the address, and the page did nothing else with them. "
        "Jarvis did not sign in."
    ),
    "failed": "The sign-in could not be completed.",
}
_POLL = 0.3
_MAX_STEPS = 4


def _outcome(result):
    return Outcome(result, _DETAILS[result])


def find_login(store, name):
    matches = [login for login in store.list() if login.name == name]
    if len(matches) != 1:
        raise LoginError("Expected exactly one saved login with that name.")
    return matches[0]


def _settle(session, clock, timeout):
    from .session import BrowserError

    deadline = clock() + timeout
    previous = None
    while (remaining := deadline - clock()) > 0:
        try:
            current = session.location(timeout=remaining)
        except BrowserError as exc:
            if exc.reason not in ("unsettled", "changed"):
                raise
            previous = None
        else:
            if clock() <= deadline and current == previous:
                return current
            previous = current
        if deadline - clock() < _POLL:
            break
        time.sleep(_POLL)
    raise BrowserError(_DETAILS["moved"], reason="moved")


def _submit(session, store, login, kind):
    from .session import BrowserError

    args = [kind, login.username if kind != "password" else ""]
    if kind == "username":
        return session.document_call(login.origin, ACT, args), None
    before = session.location()
    url = before.url
    if canonical_origin(url) != login.origin:
        raise BrowserError(_DETAILS["moved"], reason="moved")
    saved, secret = store.secret_for(login.name, url)
    try:
        # A concurrent replacement must not mix the old metadata with a new secret.
        if saved != login:
            raise LoginError("The saved login changed. Try again.")
        return session.document_call(login.origin, ACT, [*args, secret]), before
    finally:
        del secret


def sign_in(session, store, name, *, page=None, clock=time.monotonic, step_timeout=20.0) -> Outcome:
    from .session import BrowserError

    login = find_login(store, name)
    page = login.origin + "/" if page is None else page
    if canonical_origin(page) != login.origin:
        return _outcome("wrong_site")

    try:
        if session.open(page).outcome != "loaded":
            return _outcome("failed")
        after_password = False
        handled_signature = None
        # The final inspection only classifies the outcome.
        for step in range(_MAX_STEPS + 1):
            inspection = session.document_call(login.origin, INSPECT, ["after"] if step else None)
            if inspection == "moved":
                return _outcome("moved")
            kind = inspection["result"]
            if kind == "code":
                return _outcome("needs_code")
            if after_password:
                if kind == "none":
                    return _outcome("signed_in")
                if kind in ("both", "password"):
                    if handled_signature == inspection["signature"]:
                        return _outcome("wrong_destination")
                    return _outcome("rejected")
                return _outcome("failed")
            if kind == "none" and step == 0:
                return _outcome("no_form")
            if step == _MAX_STEPS or kind not in ("both", "username", "password"):
                return _outcome("failed")

            submitted, before = _submit(session, store, login, kind)
            if not isinstance(submitted, dict) or submitted["result"] not in ("submitted", "handled"):
                return _outcome("moved" if submitted == "moved" else "failed")
            landing = _settle(session, clock, step_timeout)
            after_password = before is not None
            if after_password and submitted["result"] == "handled" and (
                (before.frame_id, before.loader_id) == (landing.frame_id, landing.loader_id)
            ):
                handled_signature = submitted["before"]
            if canonical_origin(landing.url) != login.origin:
                return _outcome("signed_in" if after_password else "moved")
    except BrowserError as exc:
        return _outcome("moved" if exc.reason in ("moved", "unsettled", "not_a_site", "changed")
                        else "failed")
