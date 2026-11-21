// Tests run in Node, but the app's TypeScript settings deliberately leave Node's types out so
// browser code cannot reach for them. This declares the little a test needs to read a
// stylesheet's text, which the test runner otherwise replaces with an empty string.
declare module 'node:fs' {
  export function readFileSync(path: string, encoding: 'utf8'): string
}

interface ImportMeta {
  /** The folder of the current test file. The test runner provides it. */
  readonly dirname: string
}
