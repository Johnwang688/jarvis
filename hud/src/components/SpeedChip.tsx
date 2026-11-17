import type { Choice, ThreadModels } from "../lib/threadmodel";
import { defaultModel, entry } from "../lib/threadmodel";

const COST_FALLBACK = "Faster responses; uses more of your plan's quota";

/** The owner's next-turn speed. Native select keys stay within the chip. */
export function SpeedChip(props: {
  models: ThreadModels | null;
  choice: Choice;
  disabled?: boolean;
  pending?: boolean;
  onChange: (speed: string | null) => unknown;
}) {
  const { choice } = props;
  const model = choice.model || defaultModel(props.models, choice.provider);
  const tiers = entry(props.models, choice.provider, model)?.speeds || [];
  const bound = choice.speed_model === undefined || choice.speed_model === model;
  const selected = bound ? tiers.find((t) => t[0] === choice.speed) : undefined;
  const lit = !!choice.speed;
  const dormant = lit && !bound;
  const title = props.pending ? "Model or effort change pending; wait to choose speed"
    : !tiers.length ? "This model has no fast mode"
    : selected ? selected[2] || COST_FALLBACK
    : lit ? `Saved speed ${choice.speed} is unavailable; the next message uses Standard`
    : "Standard speed; applies from the next message";
  return (
    <span className="chip projchip speedchip" data-testid="speed-chip"
      data-pending={props.pending ? "true" : undefined} aria-busy={props.pending || undefined}
      data-on={lit ? "true" : "false"} data-unavailable={!tiers.length ? "true" : undefined} title={title}>
      <span aria-hidden="true">⚡︎</span>
      <select aria-label="Speed for this thread" data-testid="speed-chip-select"
        value={dormant ? "__saved_speed" : choice.speed ?? ""} disabled={props.disabled || props.pending || !tiers.length} title={title}
        onKeyDown={(e) => e.stopPropagation()} onKeyUp={(e) => e.stopPropagation()}
        onChange={(e) => props.onChange(e.target.value || null)}>
        <option value="">Standard</option>
        {tiers.map(([id, name, description]) => <option key={id} value={id} title={description || COST_FALLBACK}>{name}</option>)}
        {dormant ? <option disabled value="__saved_speed">{choice.speed} (unavailable)</option> : null}
        {!dormant && lit && !tiers.some((t) => t[0] === choice.speed) ? <option value={choice.speed!}>{choice.speed} (unavailable)</option> : null}
      </select>
    </span>
  );
}
