import {defineConfig} from 'vite';

// Vendor code is split into stable chunks so a change to the application does not
// invalidate the cached React, assistant-ui or markdown bundles. Total bytes are
// unchanged by splitting.
const vendorGroups = [
  {name: 'react', test: /node_modules[\\/](react|react-dom|scheduler)[\\/]/, priority: 30},
  {name: 'markdown', test: /node_modules[\\/](react-markdown|remark-[a-z-]+|rehype-[a-z-]+|micromark[a-z-]*|mdast-[a-z-]+|unified|unist-[a-z-]+|hast-[a-z-]+|vfile[a-z-]*|property-information|comma-separated-tokens|space-separated-tokens|html-url-attributes|style-to-[a-z-]+|inline-style-parser|decode-named-character-reference|character-entities[a-z-]*|estree-util-[a-z-]+|trim-lines|bail|trough|zwitch|devlop|is-plain-obj|ccount|longest-streak|markdown-table|escape-string-regexp|extend)[\\/]/, priority: 25},
  {name: 'assistant-ui', test: /node_modules[\\/]@assistant-ui[\\/]/, priority: 20},
  {name: 'icons', test: /node_modules[\\/]lucide-react[\\/]/, priority: 20},
  {name: 'vendor', test: /node_modules[\\/]/, priority: 10},
];

export default defineConfig({
  base: '/',
  build: {
    outDir: 'dist',
    rolldownOptions: {
      onwarn(warning, warn) {
        if (warning.code === 'MODULE_LEVEL_DIRECTIVE') return;
        warn(warning);
      },
      output: {codeSplitting: {groups: vendorGroups}},
    },
  },
  server: {port: 5173},
});
