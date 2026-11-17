import path from 'node:path'
import { fileURLToPath } from 'node:url'

const root = fileURLToPath(new URL('.', import.meta.url))
const repo = path.resolve(root, '..', '..', '..')
const modules = path.join(repo, 'apps', 'web', 'node_modules')

export default {
  root,
  cacheDir: path.join(modules, '.vite-console-access'),
  esbuild: { jsx: 'automatic' },
  resolve: {
    dedupe: ['react', 'react-dom'],
    alias: {
      react: path.join(modules, 'react'),
      'react-dom': path.join(modules, 'react-dom'),
      '@tanstack/react-query': path.join(modules, '@tanstack', 'react-query'),
      '@azure/msal-react': path.join(root, 'msal.ts'),
    },
  },
  server: { fs: { allow: [repo] } },
}
