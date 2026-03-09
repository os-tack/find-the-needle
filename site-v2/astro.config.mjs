import { defineConfig } from 'astro/config';

export default defineConfig({
  site: 'https://needle-bench.cc',
  output: 'static',
  build: {
    format: 'directory',
  },
});
