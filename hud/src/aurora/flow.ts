// The flow shader (R-7a, from the Bible app): one pass of domain-warped fbm noise, the lighter of the two
// styles, and the one a machine with no GPU runs. It has no memory to stir, so it ignores stirs.
import {
  applyPalette,
  createProgram,
  fillPalette,
  PALETTE,
  paletteUniforms,
  type StyleImpl,
} from './gl';
import type { AuroraPalette } from './palette';

export const FLOW_FS =
  PALETTE +
  'uniform float uTime;uniform float uAspect;\n' +
  'float vnoise(vec2 p){vec2 i=floor(p),f=fract(p);f=f*f*(3.0-2.0*f);\n' +
  ' float a=hash(i),b=hash(i+vec2(1,0)),c=hash(i+vec2(0,1)),d=hash(i+vec2(1,1));return mix(mix(a,b,f.x),mix(c,d,f.x),f.y);}\n' +
  'float fbm(vec2 p){float v=0.0,a=0.5;mat2 m=mat2(1.6,1.2,-1.2,1.6);for(int i=0;i<4;i++){v+=a*vnoise(p);p=m*p;a*=0.5;}return v;}\n' +
  'void main(){vec2 p=vec2(vUv.x*uAspect,vUv.y)*1.5;float t=uTime*0.045;\n' +
  ' vec2 q=vec2(fbm(p+vec2(0.0,t)),fbm(p+vec2(5.2,1.3)-t));\n' +
  ' vec2 r=vec2(fbm(p+3.0*q+vec2(1.7,9.2)+0.7*t),fbm(p+3.0*q+vec2(8.3,2.8)-0.6*t));\n' +
  ' float w1=smoothstep(0.42,0.78,fbm(p+2.4*r));\n' +
  ' float w2=smoothstep(0.40,0.76,fbm(p+2.4*r+vec2(3.1,7.7)));\n' +
  ' float w3=smoothstep(0.45,0.80,fbm(p*1.2+2.0*q+vec2(9.4,2.2)));\n' +
  // a hint of the drawn layout: colour 1 leans top, colour 2 the right side, colour 3 the bottom
  ' w1*=0.55+0.6*vUv.y;w2*=0.55+0.6*vUv.x;w3*=1.15-0.6*vUv.y;\n' +
  ' o=vec4(palette(clamp(vec3(w1,w2,w3)*0.95,0.0,1.0)),1.0);}';

/** The flow style. Throws if it cannot be built, with every object it made deleted. */
export function createFlow(gl: WebGL2RenderingContext, palette: AuroraPalette, w: number, h: number): StyleImpl {
  const pr = createProgram(gl, FLOW_FS);
  const pal = paletteUniforms();
  let width = w;
  let height = h;
  let time = 0;
  let released = false;

  const setPalette = (p: AuroraPalette) => {
    fillPalette(pal, p);
    gl.useProgram(pr.program);
    applyPalette(gl, pr, pal);
  };
  try {
    setPalette(palette);
  } catch (e) {
    gl.deleteProgram(pr.program);
    throw e;
  }

  return {
    style: 'flow',
    step(dt) {
      time += dt;
    },
    draw() {
      gl.bindFramebuffer(gl.FRAMEBUFFER, null);
      gl.viewport(0, 0, width, height);
      gl.useProgram(pr.program);
      gl.uniform1f(pr.uniforms.uTime ?? null, time);
      gl.uniform1f(pr.uniforms.uAspect ?? null, width / height);
      gl.uniform2f(pr.uniforms.texel ?? null, 0, 0);
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
    },
    setPalette,
    resize(nw, nh) {
      width = nw;
      height = nh;
      return true;
    },
    stir() {},
    ripple() {},
    release() {
      if (released) return;
      released = true;
      gl.deleteProgram(pr.program);
    },
  };
}
