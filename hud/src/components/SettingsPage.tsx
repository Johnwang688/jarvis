import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { SETTINGS_SECTIONS, type SettingsSection } from "../lib/settings";
import { CrashProbe } from "./Boundary";

/** Covers the work without owning its lifetime. Nested dialogs and approval
 * cards remain separate siblings, above this page. */
export function SettingsPage(props: {
  section: SettingsSection; onSection: (section: SettingsSection) => void;
  onClose: () => void; blocked: boolean; nested: boolean; zoom: number; children: ReactNode;
}) {
  const page = useRef<HTMLElement>(null);
  const nestedWas = useRef(props.nested);
  const [top, setTop] = useState(28);
  // The titlebar can wrap at large zoom. Convert its actual screen edge
  // into the zoomed root's coordinates; no viewport-unit guess.
  useLayoutEffect(() => {
    const title = document.getElementById("titlebar");
    if (!title) return;
    const measure = () => setTop(title.getBoundingClientRect().bottom / (props.zoom / 100));
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(title);
    window.addEventListener("resize", measure);
    return () => { observer.disconnect(); window.removeEventListener("resize", measure); };
  }, [props.zoom]);
  useLayoutEffect(() => {
    if (!props.blocked) page.current?.focus({ preventScroll: true });
  }, []);
  useLayoutEffect(() => {
    if (!props.nested && nestedWas.current && !props.blocked)
      page.current?.querySelector<HTMLElement>('[data-testid="model-pin-more"]')?.focus({ preventScroll: true });
    nestedWas.current = props.nested;
  }, [props.nested, props.blocked]);
  useEffect(() => {
    const key = (e: KeyboardEvent) => {
      // Bubble phase lets archive confirmations and catalogue controls own
      // Escape first. A card's Escape belongs only to the card.
      if (e.key !== "Escape" || e.defaultPrevented || props.blocked || props.nested) return;
      e.preventDefault();
      props.onClose();
    };
    document.addEventListener("keydown", key);
    return () => document.removeEventListener("keydown", key);
  }, [props.blocked, props.nested, props.onClose]);
  return (
    <section ref={page} className="settings-page" data-testid="settings-page"
      aria-label="Settings" tabIndex={-1} style={{ top }}
      {...((props.nested || props.blocked) ? { inert: "" } as Record<string, string> : {})}>
      <CrashProbe where="settings" />
      <header className="settings-heading">
        <h1>Settings</h1>
        <button type="button" data-testid="settings-close" aria-label="Close Settings"
          disabled={props.blocked || props.nested} onClick={props.onClose}>×</button>
      </header>
      <div className="settings-layout">
        <nav className="settings-nav" aria-label="Settings sections">
          {SETTINGS_SECTIONS.map((section) => (
            <button type="button" key={section} data-testid={`settings-${section.toLowerCase()}`}
              aria-current={props.section === section ? "page" : undefined}
              disabled={props.blocked || props.nested}
              onClick={() => props.onSection(section)}>{section}</button>
          ))}
        </nav>
        <div className="settings-content" data-testid="settings-content">
          <h2>{props.section}</h2>
          {props.children}
        </div>
      </div>
    </section>
  );
}
