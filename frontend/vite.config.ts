import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Relative base: pywebview serves dist/ from a loopback bottle server on a
// random port, so nothing may assume it sits at the document root.
export default defineConfig({
  plugins: [react()],
  base: './',
  server: { strictPort: true },
})
