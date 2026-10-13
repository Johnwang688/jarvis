import { describe, expect, it } from "vitest";
import { SendStops } from "./sendStops";
describe("stops before send acknowledgment", () => {
  it("keeps one stop across placeholder binding and repeated presses", () => {
    const s = new SendStops(); s.begin("local-1", null);
    expect(s.stop("local-1")).toBe(true); s.bind("local-1", "t1");
    expect(s.stop("t1")).toBe(true);
    expect(s.acknowledge("local-1", {status:"started",turn_id:"a"})).toEqual({threadId:"t1",turnIds:["a"]});
    expect(s.acknowledge("local-1", {turn_id:"b"})).toBeNull();
    expect(s.stop("t1")).toBe(false);
  });
  it("also waits for an already known thread's send, without redirecting to another", () => {
    const s = new SendStops();s.begin("one", "t1");s.begin("two", "t2");
    expect(s.stop("t1")).toBe(true);
    expect(s.acknowledge("two",{status:"started",turn_id:"b"})).toBeNull();
    expect(s.acknowledge("one",{status:"started",turn_id:"a"})).toEqual({threadId:"t1",turnIds:["a"]});
  });
  it("clears failed/dropped sends and never invents an unguarded interrupt", () => {
    const s = new SendStops();s.begin("one","t1");s.stop("one");s.finish("one");
    expect(s.stop("t1")).toBe(false);
    s.begin("one","t1");s.stop("one");expect(s.acknowledge("one",{status:"dropped"})).toBeNull();
    s.begin("one","t1");s.stop("one");expect(s.acknowledge("one",{status:"started"})).toEqual({threadId:"t1",turnIds:[]});
  });
  it("names only the running and this message's queued turn when a stale snapshot queued it", () => {
    const s = new SendStops();s.begin("one","t1");s.stop("t1");
    expect(s.acknowledge("one",{status:"queued",turn_id:"running",queued_turn_id:"mine"})).toEqual({threadId:"t1",turnIds:["running","mine"]});
  });
});
