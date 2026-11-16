// Phone modals are siblings under one host. Only the top layer handles focus and keys;
// its siblings stay inert until the stack releases them, preserving prior inert state.
interface Layer {
  root: HTMLElement;
  focus: () => HTMLElement | null | undefined;
  restore: () => HTMLElement | null | undefined;
  dismiss: () => void;
  opener?: HTMLElement | null;
}
export interface Overlay {
  close: () => void;
}
const stacks = new WeakMap<HTMLElement, OverlayStack>();

export function openOverlay(host: HTMLElement, root: HTMLElement, options: Omit<Layer, "root" | "opener">): Overlay {
  let stack = stacks.get(host);
  if (!stack) { stack = new OverlayStack(host); stacks.set(host, stack); }
  return stack.open({ root, ...options });
}
export function refreshOverlays(host: HTMLElement): boolean {
  return stacks.get(host)?.refresh() ?? false;
}

class OverlayStack {
  private layers: Layer[] = [];
  private prior = new Map<HTMLElement, boolean>();
  constructor(private host: HTMLElement) {}
  private get top(): Layer | undefined { return this.layers.at(-1); }
  open(layer: Layer): Overlay {
    layer.opener = layer.restore() ?? null;
    if (!this.layers.length) {
      document.addEventListener("keydown", this.keys, true);
      document.addEventListener("focusin", this.focus, true);
    }
    this.layers.push(layer);
    this.refresh();
    return { close: () => this.close(layer) };
  }
  refresh(): boolean {
    const blocked = new Set(Array.from(this.host.children)
      .filter((node): node is HTMLElement => node instanceof HTMLElement && !!this.top && node !== this.top.root));
    for (const [node, inert] of this.prior) {
      if (!blocked.has(node)) { node.toggleAttribute("inert", inert); this.prior.delete(node); }
    }
    for (const node of blocked) {
      if (!this.prior.has(node)) this.prior.set(node, node.hasAttribute("inert"));
      node.setAttribute("inert", "");
    }
    return !!this.top;
  }
  private close(layer: Layer): void {
    const index = this.layers.indexOf(layer);
    if (index < 0) return;
    const wasTop = this.top === layer;
    // If a lower layer goes away first, its successors return to its opener instead.
    for (const above of this.layers.slice(index + 1)) {
      if (above.opener && layer.root.contains(above.opener)) {
        above.restore = layer.restore;
        above.opener = layer.opener;
      }
    }
    this.layers.splice(index, 1);
    this.refresh();
    if (!this.top) {
      document.removeEventListener("keydown", this.keys, true);
      document.removeEventListener("focusin", this.focus, true);
    }
    if (!wasTop) return;
    const target = layer.restore();
    if (target?.isConnected && !target.closest("[inert]")) target.focus({ preventScroll: true });
    else this.top?.focus()?.focus({ preventScroll: true });
  }
  private focus = (event: FocusEvent) => {
    const top = this.top;
    if (top && !top.root.contains(event.target as Node)) top.focus()?.focus({ preventScroll: true });
  };
  private keys = (event: KeyboardEvent) => {
    const top = this.top;
    if (!top) return;
    if (event.key === "Escape") {
      event.preventDefault(); event.stopImmediatePropagation(); top.dismiss();
    }
    if (event.key !== "Tab") return;
    const buttons = Array.from(top.root.querySelectorAll<HTMLElement>(
      'button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), a[href], [tabindex]'))
      .filter((node) => node.tabIndex >= 0 && !node.closest("[hidden], [inert]"));
    const first = buttons[0], last = buttons.at(-1), active = document.activeElement;
    if (!buttons.includes(active as HTMLElement) || (event.shiftKey ? active === first : active === last)) {
      event.preventDefault();
      (event.shiftKey ? last : first)?.focus({ preventScroll: true });
    }
  };
}
