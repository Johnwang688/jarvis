import { useEffect, useRef } from "react";
import { renderMarkdownInto } from "../lib/markdown";

/**
 * His markdown, rendered. The sanitizing lives in lib/markdown.ts so no call
 * site can forget it — the same rule voice.tts() applies to speakable().
 */
export function Markdown({ text, className }: { text: string; className?: string }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (ref.current) renderMarkdownInto(ref.current, text);
  }, [text]);
  return <div className={className || "body"} ref={ref} />;
}
