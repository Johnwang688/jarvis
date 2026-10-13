"""Bounded, value-free action facts in BR5's existing isolated world."""
from __future__ import annotations
from dataclasses import dataclass
import hashlib
import json
import math

from jarvis.browser_visibility import _JUDGE_JS
from .act_rules import Element, Form
from .look import LookRefused, guarded
from .look_scan import SCAN_JS

# A fresh full bounded visible scan guards raw page text/attributes before any
# classifier truncation. Element facts never touch value/defaultValue.
PREPARE_JS = r"""(args) => {
const base = (""" + SCAN_JS + r""")(args);
if (base.incomplete || base.changed) return base;
""" + _JUDGE_JS + r"""
const state=globalThis.__jarvisLook;
if (!state || state.serial!==args.serial) return {changed:true};
let size=base.raw.reduce((n,s)=>n+s.length,0), visits=0;
const charge=v=>{
    const text=String(v||'');size+=text.length;
    if(size>args.rawCap) throw Error('bounded scan');
    base.raw.push(text);return text;
};
const parent=e=>e.parentElement || (e.getRootNode() instanceof ShadowRoot ? e.getRootNode().host : null);
const visible=e=>{
    if(!e || !e.isConnected) return false;
    for(let a=e;a;a=parent(a)) {
        const s=css(a);
        if(s.display==='none'||s.visibility!=='visible'||parseFloat(s.opacity)<=.05||hides(a))return false;
    }
    return true;
};
const textOf=e=>{
    if(!e || !visible(e))return '';
    const parts=[], stack=[e];
    while(stack.length) {
        const n=stack.pop();if(++visits>args.nodeCap)throw Error('bounded scan');
        if(n.nodeType===Node.TEXT_NODE) {
            const p=n.parentElement||(n.parentNode instanceof ShadowRoot?n.parentNode.host:null);
            if(p && !p.isContentEditable && visible(p)) parts.push(charge(n.nodeValue));
        } else if(n.nodeType===Node.ELEMENT_NODE) {
            if(n.isContentEditable||/^(INPUT|TEXTAREA|SELECT|SCRIPT|STYLE|TEMPLATE|NOSCRIPT|DATALIST|IFRAME|FRAME|CANVAS)$/.test(n.tagName))continue;
            if(!visible(n))continue;
            const kids=Array.from(n.childNodes);
            if(n.shadowRoot)kids.push(...n.shadowRoot.childNodes);
            if(kids.length>args.nodeCap-visits)throw Error('bounded scan');
            for(let i=kids.length-1;i>=0;i--)stack.push(kids[i]);
        }
    }
    return charge(parts.join(' '));
};
const attr=(e,k)=>charge(e.getAttribute(k)||'');
const frameOrigin=e=>{
    const src=attr(e,'src');
    if(!src)return '';
    try{return charge(new URL(src,location.href).origin)}catch{return charge('unknown')}
};
const described=e=>{
    const ids=attr(e,'aria-labelledby').trim().split(/\s+/).filter(Boolean);
    if(ids.length>32)throw Error('bounded scan');
    return charge(ids.map(id=>textOf(e.getRootNode().getElementById?.(id))).join(' '));
};
const element=e=>{
    const tag=e.tagName.toLowerCase(), labels=Array.from(e.labels||[]);
    if(labels.length>32)throw Error('bounded scan');
    const label=charge(labels.map(textOf).join(' '));
    const type=attr(e,'type'),role=attr(e,'role'),aria=attr(e,'aria-label');
    const max=attr(e,'maxlength');
    const href=tag==='a'?charge(e.href):'',src=/^(iframe|frame)$/.test(tag)?charge(e.src):'';
    const headingParent=parent(e);
    const headings=headingParent?headingParent.querySelectorAll('h1,h2,h3,h4,h5,h6,[role=heading]'):[];
    if(headings.length>args.nodeCap)throw Error('bounded scan');
    let heading='';
    for(const h of headings) {
        if(h===e||h.contains(e)||!visible(h))continue;
        if(h.compareDocumentPosition(e)&Node.DOCUMENT_POSITION_FOLLOWING)heading=textOf(h);
    }
    let siblings=0;
    if(headingParent)for(const n of headingParent.querySelectorAll('input[maxlength]')) {
        if(++visits>args.nodeCap)throw Error('bounded scan');
        if(n.getAttribute('maxlength')==='1')siblings++;
    }
    return {tag,type,role,name:charge(aria||described(e)||label||textOf(e)),
        label,placeholder:attr(e,'placeholder'),aria_label:aria,
        attr_name:attr(e,'name'),attr_id:attr(e,'id'),autocomplete:attr(e,'autocomplete'),
        inputmode:attr(e,'inputmode'),maxlength:/^\d{1,8}$/.test(max)?Number(max):null,
        editable:e.isContentEditable,href,src,
        submits:!!e.form && (tag==='button'&&(type===''||type.toLowerCase()==='submit') ||
                           tag==='input'&&/^(submit|image)$/i.test(type)),
        heading,single_char_siblings:siblings,frame_origin:''};
};
let focused=document.activeElement;
while(focused && focused.shadowRoot && focused.shadowRoot.activeElement)focused=focused.shadowRoot.activeElement;
if(args.action==='press' && focused && /^(IFRAME|FRAME)$/.test(focused.tagName))
    return {refused:'focused-frame'};
const e=args.action==='press'?(focused===document.body||focused===document.documentElement?null:focused):
    (args.target?state.refs.get(args.target):null);
if(args.target && (!e || !e.isConnected))return {changed:true};
let rect=null,target=null,form=null,options=[];
if(e) {
    if(!visible(e))return {changed:true};
    const r=e.getBoundingClientRect();
    if(r.width<8||r.height<8)return {changed:true};
    rect=[r.left,r.top,r.right,r.bottom];
    target=element(e);
    const actual=e.form||e.closest('form');
    const region=actual||parent(e);
    const fields=actual?Array.from(actual.elements):Array.from(region?.querySelectorAll('input,textarea,select,button,[contenteditable],[role=textbox]')||[]);
    const frames=Array.from(region?.querySelectorAll('iframe,frame')||[]);
    if(fields.length>500||frames.length>500)throw Error('bounded scan');
    form={method:actual?charge(actual.method):'',action:actual?charge(actual.action):'',
          fields:fields.map(element),frame_origins:frames.map(frameOrigin)};
    if(e.tagName==='SELECT') {
        if(e.options.length>500)throw Error('bounded scan');
        options=Array.from(e.options).map((option,index)=>({index,label:charge(option.getAttribute('label')||option.textContent||''),
            disabled:!!option.disabled||!!option.closest('optgroup[disabled]')}));
    }
}
base.target=target;base.form=form;base.rect=rect;base.options=options;
base.context=charge(base.lines.join('\n'));
base.focusedTarget=!!e&&focused===e;
return base;
}"""


@dataclass(frozen=True)
class Prepared:
    serial: str
    target_ref: str
    target: Element | None
    form: Form | None
    context: str
    context_flagged: bool
    rect: tuple[float, float, float, float] | None
    fingerprint: str
    options: tuple[tuple[int, str, bool], ...]
    focused_target: bool
    url: str
    title: str


def _element(row):
    if row is None:
        return None
    if not isinstance(row, dict) or set(row) != set(Element.__dataclass_fields__):
        raise LookRefused("the Browser target could not be checked safely")
    strings=set(row)-{"maxlength","editable","submits","single_char_siblings"}
    if any(not isinstance(row[key],str) for key in strings):
        raise LookRefused("the Browser target could not be checked safely")
    if (type(row["editable"]) is not bool or type(row["submits"]) is not bool
            or type(row["single_char_siblings"]) is not int
            or row["single_char_siblings"]<0
            or (row["maxlength"] is not None and (type(row["maxlength"]) is not int or row["maxlength"]<0))):
        raise LookRefused("the Browser target could not be checked safely")
    return Element(**row)


def prepare_result(raw, target_ref, forms) -> Prepared:
    if not isinstance(raw,dict) or raw.get("changed") or raw.get("refused"):
        raise LookRefused("the Browser target changed or cannot be checked; look again")
    withheld=guarded(raw,forms)
    target=_element(raw.get("target"));form=None
    if raw.get("form") is not None:
        data=raw["form"]
        if (not isinstance(data,dict) or set(data)!={"method","action","fields","frame_origins"}
                or not isinstance(data["method"],str) or not isinstance(data["action"],str)
                or not isinstance(data["fields"],list) or len(data["fields"])>500
                or not isinstance(data["frame_origins"],list) or len(data["frame_origins"])>500
                or any(not isinstance(origin,str) for origin in data["frame_origins"])):
            raise LookRefused("the Browser form could not be checked safely")
        fields=tuple(_element(row) for row in data["fields"])
        if any(e is None for e in fields):raise LookRefused("the Browser form could not be checked safely")
        form=Form(data["method"],data["action"],fields,tuple(data["frame_origins"]))
    rect=raw.get("rect")
    if rect is not None:
        if (not isinstance(rect,list) or len(rect)!=4
                or any(type(n) not in (int,float) or not math.isfinite(n) for n in rect)
                or rect[2]<=rect[0] or rect[3]<=rect[1]):
            raise LookRefused("the Browser target geometry changed; look again")
        rect=tuple(rect)
    options=raw.get("options")
    if not isinstance(options,list) or len(options)>500:raise LookRefused("the Browser options could not be checked safely")
    for row in options:
        if (not isinstance(row,dict) or set(row)!={"index","label","disabled"} or type(row["index"]) is not int
                or row["index"]<0 or not isinstance(row["label"],str) or type(row["disabled"]) is not bool):
            raise LookRefused("the Browser options could not be checked safely")
    for key in ("serial","url","title","context"):
        if not isinstance(raw.get(key),str):raise LookRefused("the Browser target could not be checked safely")
    if type(raw.get("focusedTarget")) is not bool:raise LookRefused("the Browser focus could not be checked safely")
    # Include full guarded attributes, context and option labels. Geometry and
    # focus may change only through the separately checked execution path.
    identity={key:raw.get(key) for key in ("serial","url","title","target","form","context","options")}
    digest=hashlib.sha256(json.dumps(identity,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()).hexdigest()
    return Prepared(raw["serial"],target_ref,target,form,raw["context"],withheld,rect,digest,
        tuple((row["index"],row["label"],row["disabled"]) for row in options),
        raw["focusedTarget"],raw["url"],raw["title"])

