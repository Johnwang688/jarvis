/// <reference types="vite/client" />

// Vite's `?worker` suffix imports are resolved by the bundler, not by tsc, so
// the module shape is declared here rather than left as an error.
declare module "*?worker" {
  const WorkerFactory: new () => Worker;
  export default WorkerFactory;
}
