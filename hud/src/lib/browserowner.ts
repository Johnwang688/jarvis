// One ordered publisher owns all Browser front/permission/address writes.
// A layout change aborts a slow owner navigation request and publishes a newer
// sequence immediately. The daemon generation fence cancels the old result;
// waiting for Chrome here would leave a hidden pane discoverable until it ended.
import { judgeBrowserUrl, type DaemonPorts } from "./browser";
import { orderOf, type BrowserSource, type PaneNo, type Workspace } from "./workspace";

export interface FrontTab {
  id: string; mode: "direct" | "remote"; source: BrowserSource | null;
  url?: string; tab_id?: string | null;
}
export interface FrontPane {
  pane_id: number; slot: number | null; drawn: boolean; profile: string; ctx: string | null;
  see: boolean; drive: boolean; active: string | null; tabs: FrontTab[];
}
export interface BrowserFront {
  window: string; focus_seq: number; focused_slot: number | null; covered: boolean; panes: FrontPane[];
}
export interface BrowserReceipt {
  window: string; seq: number;
  panes: {pane_id:number;see:boolean;drive:boolean;tabs:{id:string;view_id:string;generation:number}[]}[];
  tab_id?: string | null; ctx?: string | null; url?: string;
}
export type BrowserWrite = (path:string, method:"POST"|"PATCH", body:Record<string,unknown>, signal:AbortSignal) => Promise<BrowserReceipt>;
let sequence = 0; // shared across React mount lifetimes; never persisted
const cancelled = () => new Error("That Browser action was cancelled because the pane changed.");

export function browserFront(ws: Workspace, drawn: readonly PaneNo[], covered: boolean,
  window: string, profile: string | null, focused: PaneNo | null, focusSeq: number,
  ports: DaemonPorts, privateDrive: ReadonlyMap<number, string> = new Map()): BrowserFront {
  const order = orderOf(ws);
  const retained = new Set([...order, ...ws.kept]);
  const panes: FrontPane[] = [];
  ws.panes.forEach((spec, index) => {
    const pid = (index + 1) as PaneNo;
    const selectedProfile = spec.browserProfile ?? profile;
    if (!retained.has(pid) || spec.view !== "browser" || !selectedProfile) return;
    const tabs = (spec.browser?.tabs ?? []).map((tab): FrontTab => {
      const source = tab.source ?? (tab.auto ? {kind:"unknown" as const} : null);
      if (tab.mode === "remote" || selectedProfile === "private")
        return {id:tab.id,mode:"remote",tab_id:tab.remoteTabId ?? null,source};
      const judged = judgeBrowserUrl(tab.url, ports);
      return {id:tab.id,mode:"direct",url:judged.ok ? judged.url : "",source};
    });
    panes.push({pane_id:pid,slot:order.includes(pid) ? order.indexOf(pid)+1 : null,
      drawn:drawn.includes(pid),profile:selectedProfile,ctx:spec.browserCtx ?? null,
      see:spec.see !== false,drive:spec.see !== false && (selectedProfile === "private"
        ? !!spec.browserCtx && privateDrive.get(pid) === spec.browserCtx : spec.drive !== false),active:spec.browser?.active || null,tabs});
  });
  const chosen = panes.find((p) => p.pane_id === focused && p.drawn);
  return {window,focus_seq:focusSeq,focused_slot:covered ? null : chosen?.slot ?? null,covered,panes};
}

interface Action {
  path: string; method:"POST"|"PATCH"; body: Record<string,unknown>; revision:number;
  apply: (reply:BrowserReceipt, front:BrowserFront) => BrowserFront;
  resolve:(value:BrowserReceipt)=>void; reject:(reason:Error)=>void;
}

export class BrowserPublisher {
  private latest: BrowserFront | null = null;
  private encoded = "";
  private revision = 0;
  private dirty = false;
  private online = false;
  private active: {abort:AbortController;revision:number;action:Action|null} | null = null;
  private actions: Action[] = [];
  constructor(private write:BrowserWrite, private receipt:(reply:BrowserReceipt,front:BrowserFront)=>void,
    private failed:(message:string)=>void, private next:()=>number = () => ++sequence) {}

  connect(online:boolean) {
    this.online = online;
    if (!online) {
      this.revision++;
      this.active?.abort.abort();
      this.active?.action?.reject(cancelled());
      this.active = null;
      for (const action of this.actions.splice(0)) action.reject(cancelled());
    } else { this.dirty = !!this.latest; void this.pump(); }
  }

  update(front:BrowserFront) {
    const encoded = JSON.stringify(front);
    if (encoded === this.encoded) return;
    this.latest = JSON.parse(encoded) as BrowserFront;
    this.encoded = encoded;
    this.revision++;
    this.dirty = true;
    // Abort a slow prior mutation/report; a newer sequence is its cancellation
    // at the host, even when Chrome cannot cancel its already-started load.
    this.active?.abort.abort();
    this.active?.action?.reject(cancelled());
    this.active = null;
    for (const action of this.actions.splice(0)) action.reject(cancelled());
    void this.pump();
  }

  heartbeat() { this.dirty = !!this.latest; void this.pump(); }
  dispose() { this.connect(false); this.latest = null; this.encoded = ""; }

  mutate(path:string, method:"POST"|"PATCH", body:Record<string,unknown>,
    apply:(reply:BrowserReceipt,front:BrowserFront)=>BrowserFront):Promise<BrowserReceipt> {
    if (!this.online || !this.latest) return Promise.reject(new Error("Wait for the owner window to reconnect."));
    if (this.actions.length >= 4) return Promise.reject(new Error("Wait for the current Browser action."));
    return new Promise((resolve,reject) => {
      this.actions.push({path,method,body,apply,revision:this.revision,resolve,reject});
      void this.pump();
    });
  }

  private async pump() {
    if (this.active || !this.online || !this.latest || (!this.dirty && !this.actions.length)) return;
    const action = this.dirty ? null : this.actions.shift()!;
    if (action && action.revision !== this.revision) { action.reject(cancelled()); void this.pump(); return; }
    const front = this.latest;
    const seq = this.next();
    const current = {abort:new AbortController(),revision:this.revision,action};
    this.active = current;
    if (!action) this.dirty = false;
    const path = action?.path ?? "/browser/front";
    const method = action?.method ?? "POST";
    const body = action ? {...action.body,window:front.window,seq} : {...front,seq};
    let succeeded = false;
    try {
      const reply = await this.write(path,method,body,current.abort.signal);
      if (this.active !== current || current.abort.signal.aborted || current.revision !== this.revision || !this.online) throw cancelled();
      if (reply.window !== front.window || reply.seq !== seq || !Array.isArray(reply.panes)) throw new Error("The Browser reply did not match this window.");
      if (action) {
        this.latest = action.apply(reply, this.latest!);
        this.encoded = JSON.stringify(this.latest);
      }
      this.receipt(reply,this.latest!);
      action?.resolve(reply);
      succeeded = true;
    } catch (error) {
      const message = error instanceof Error ? error.message : "The Browser change could not be applied.";
      action?.reject(error instanceof Error ? error : new Error(message));
      if (this.active === current && !current.abort.signal.aborted) {
        // The host may have applied none or all of a failed write. Require a
        // fresh full report before any later mutation, including a new gesture.
        this.dirty = true;
        this.failed(message);
        // Do not retry from finally after failure. A new state, owner action,
        // heartbeat or reconnect resumes; existing queued changes are refused.
        for (const queued of this.actions.splice(0)) queued.reject(new Error(message));
      }
    } finally {
      if (this.active === current) { this.active = null; if (succeeded) void this.pump(); }
    }
  }
}
