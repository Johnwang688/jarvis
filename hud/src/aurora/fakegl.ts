// A fake WebGL2 context for the aurora's tests (jsdom has none): tracks live objects and screen draws, can
// be lost and restored, and can pretend to be software or to lack half-float targets.
export interface FakeGL {
  gl: WebGL2RenderingContext;
  state: { lost: boolean; renderer: string; halfFloat: boolean; screenDraws: number; stirCalls: number;
    /** The last value set for each scalar float uniform (`uCurlK`, `uDiss`, ...), by name. */ uniforms: Record<string, number>;
    /** Every `uColors[0]` vec3 array uploaded, oldest first (the fluid sends the velocity splat, then the dye splat, each step). */ colours: number[][];
    /** Every `uPoints[0]` vec2 array uploaded, oldest first: the emitter positions, once per splat. */ points: number[][] };
  /** GL objects created and not deleted. */
  live(): number;
  lose(): void;
  restore(): void;
}

export function fakeGL(opts: { renderer?: string; halfFloat?: boolean } = {}): FakeGL {
  const state = { lost: false, renderer: opts.renderer ?? "NVIDIA GeForce", halfFloat: opts.halfFloat ?? true, screenDraws: 0, stirCalls: 0, uniforms: {} as Record<string, number>, colours: [] as number[][], points: [] as number[][] };
  const sources = new Map<object, string>();
  const attached = new Map<object, object[]>();
  const active = new Map<object, string[]>();
  const live = new Set<object>();
  let boundFb: unknown = null;
  const consts = new Map<string, number>();
  const constant = (name: string) => {
    if (!consts.has(name)) consts.set(name, 1000 + consts.size);
    return consts.get(name)!;
  };
  const impl: Record<string, (...a: unknown[]) => unknown> = {
    isContextLost: () => state.lost,
    getExtension: (n) => {
      if (state.lost) return null;
      if (n === "WEBGL_debug_renderer_info") return { UNMASKED_RENDERER_WEBGL: 0x9246 };
      if (n === "EXT_color_buffer_float") return state.halfFloat ? {} : null;
      if (n === "EXT_color_buffer_half_float") return state.halfFloat ? {} : null;
      // A controller's destroy releases its context with this; the fake context is shared by every controller a
      // test makes (each has a canvas, and so a context, in a browser), so it must not be lost by that. A test
      // loses it on purpose with lose().
      if (n === "WEBGL_lose_context") return { loseContext: () => undefined };
      return {};
    },
    getParameter: () => (state.lost ? null : state.renderer),
    getShaderParameter: () => !state.lost,
    getProgramParameter: (p, k) => (k === constant("ACTIVE_UNIFORMS") ? (active.get(p as object)?.length ?? 0) : !state.lost),
    shaderSource: (sh, src) => void sources.set(sh as object, String(src)),
    attachShader: (p, sh) => void attached.set(p as object, [...(attached.get(p as object) ?? []), sh as object]),
    linkProgram: (p) => {
      const names: string[] = [];
      for (const sh of attached.get(p as object) ?? []) {
        for (const m of (sources.get(sh) ?? "").matchAll(/uniform\s+\w+\s+(\w+)(\[\d+\])?\s*;/g)) names.push(m[2] ? `${m[1]}[0]` : m[1]!);
      }
      active.set(p as object, names);
    },
    uniform1f: (loc, v) => void ((loc as { name?: string } | null)?.name && (state.uniforms[(loc as { name: string }).name] = v as number)),
    uniform2fv: (loc, v) => {
      if ((loc as { name?: string } | null)?.name === "uPoints[0]") state.points.push(Array.from(v as ArrayLike<number>));
    },
    uniform3fv: (loc, v) => {
      if ((loc as { name?: string } | null)?.name === "uColors[0]") state.colours.push(Array.from(v as ArrayLike<number>));
    },
    checkFramebufferStatus: () => (state.lost ? 0 : constant("FRAMEBUFFER_COMPLETE")),
    getActiveUniform: (p, i) => {
      const name = active.get(p as object)?.[i as number];
      return name ? { name } : null;
    },
    getUniformLocation: (_p, name) => ({ name }),
    getShaderInfoLog: () => "",
    getProgramInfoLog: () => "",
    bindFramebuffer: (_t, fb) => void (boundFb = fb),
    drawArrays: () => {
      if (boundFb === null && !state.lost) state.screenDraws++;
    },
  };
  for (const kind of ["Shader", "Program", "Buffer", "Texture", "Framebuffer"]) {
    impl[`create${kind}`] = () => {
      if (state.lost) return null;
      const o = {};
      live.add(o);
      return o;
    };
    impl[`delete${kind}`] = (o) => void live.delete(o as object);
  }
  const gl = new Proxy({} as Record<string, unknown>, {
    get(t, name: string) {
      // A test may wrap a method by assigning it on the proxy (the RGB cycle test spies on uniform3fv).
      if (Object.prototype.hasOwnProperty.call(t, name)) return t[name];
      if (name in impl) return impl[name];
      if (/^[A-Z][A-Z0-9_]+$/.test(name)) return constant(name);
      return () => undefined;
    },
  }) as unknown as WebGL2RenderingContext;
  return {
    gl,
    state,
    live: () => live.size,
    lose() {
      state.lost = true;
      live.clear();
    },
    restore() {
      state.lost = false;
    },
  };
}
