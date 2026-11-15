// The Inbox strip can wrap as counts, width and Dynamic Type change. Reserve
// its actual height in the scrollport, so every descendant's native jump
// (including focus and anchors) uses the same unobscured area.
export class RowScroll {
  private strip: HTMLElement | null = null;
  private observer: ResizeObserver | null = null;

  constructor(private readonly main: HTMLElement) {}

  /** Run after mounting a view and before a jump: ResizeObserver is asynchronous. */
  sync(): number {
    const strip = this.main.querySelector<HTMLElement>(".m-strip");
    if (strip !== this.strip) {
      this.observer?.disconnect();
      this.strip = strip;
      if (strip && typeof ResizeObserver !== "undefined") {
        this.observer = new ResizeObserver(() => this.sync());
        this.observer.observe(strip);
      }
    }
    const height = strip?.getBoundingClientRect().height ?? 0;
    if (!height) {
      this.main.style.removeProperty("--m-row-scroll-top");
      return 0;
    }
    const rem = parseFloat(getComputedStyle(this.main.ownerDocument.documentElement).fontSize) || 16;
    // Sticky starts at the scrollport's padded edge, below its border box top.
    const padding = parseFloat(getComputedStyle(this.main).paddingTop) || 0;
    // Keep the strip's existing gap below it, in the reader's rem units.
    this.main.style.setProperty("--m-row-scroll-top", `${(height + padding) / rem + 0.625}rem`);
    return height + padding + 0.625 * rem;
  }

  jump(row: HTMLElement): void {
    this.sync();
    row.scrollIntoView({ block: "start" });
  }

  /** Preserve restored scroll when visible; otherwise expose the row's start. */
  returnTo(row: HTMLElement): void {
    const inset = this.sync();
    const area = this.main.getBoundingClientRect();
    const at = row.getBoundingClientRect();
    const top = area.top + inset;
    if (at.top < top || at.bottom > area.bottom) {
      row.scrollIntoView({ block: at.top < top || at.height > area.bottom - top ? "start" : "nearest" });
    }
  }

  stop(): void {
    this.observer?.disconnect();
    this.observer = null;
    this.strip = null;
    this.main.style.removeProperty("--m-row-scroll-top");
  }
}
