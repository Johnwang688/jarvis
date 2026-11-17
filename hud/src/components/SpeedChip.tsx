import type { Choice, ThreadModels } from "../lib/threadmodel";
import { defaultModel, entry } from "../lib/threadmodel";

/** The owner's next-turn speed. Native select keys stay within the chip. */
export function SpeedChip(props: {
  models: ThreadModels | null;
  choice: Choice;
  disabled?: boolean;
  onChange: (speed: string | null) => unknown;
}) {
  const { choice } = props;
  const tiers = entry(props.models, choice.provider,
    choice.model || defaultModel(props.models, choice.provider))?.speeds || [];
  const selected = tiers.find((t) => t[0] === choice.speed);
  const lit = !!choice.speed;
  const title = !tiers.length ? "This model has no fast mode"
    : selected ? selected[2]
    : lit ? `Saved speed ${choice.speed} is unavailable; the next message uses Standard`
    : "Standard speed; applies from the next message";
  return (
    <span className="chip projchip speedchip" data-testid="speed-chip"
      data-on={lit ? "true" : "false"} data-unavailable={!tiers.length ? "true" : undefined} title={title}>
      <span aria-hidden="true">⚡︎</span>
      <select aria-label="Speed for this thread" data-testid="speed-chip-select"
        value={choice.speed ?? ""} disabled={props.disabled || !tiers.length} title={title}
        onKeyDown={(e) => e.stopPropagation()} onKeyUp={(e) => e.stopPropagation()}
        onChange={(e) => props.onChange(e.target.value || null)}>
        <option value="">Standard</option>
        {tiers.map(([id, name, description]) => <option key={id} value={id} title={description}>{name}</option>)}
        {lit && !selected ? <option value={choice.speed!}>{choice.speed} (unavailable)</option> : null}
      </select>
    </span>
  );
}
