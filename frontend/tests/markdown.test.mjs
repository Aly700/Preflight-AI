import test from 'node:test';
import assert from 'node:assert/strict';
import {createElement} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import Markdown from 'react-markdown';
const load = async () => {
  try { return await import('../src/markdown.mjs'); }
  catch { assert.fail('The answer markdown options are not implemented'); }
};
const render = async text => {
  const {remarkPlugins} = await load();
  return renderToStaticMarkup(createElement(Markdown, {remarkPlugins}, text));
};
test('A table in an answer renders as a table with a header row', async () => {
  const html = await render('| Issue | Priority |\n|---|---|\n| ISSUE-0001 | High |\n| ISSUE-0003 | Low |');
  assert.match(html, /<table>/);
  assert.match(html, /<thead>\s*<tr>\s*<th>Issue<\/th>/);
  assert.equal(html.match(/<tr>/g).length, 3);
});
test('Strikethrough, task lists and bare links follow GitHub-flavoured markdown', async () => {
  const html = await render('~~retired~~\n\n- [x] checked\n- [ ] open\n\nSee https://example.org/records');
  assert.match(html, /<del>retired<\/del>/);
  assert.match(html, /<input type="checkbox" disabled="" checked=""\/>/);
  assert.match(html, /<a href="https:\/\/example\.org\/records">/);
});
test('Raw HTML and script links in an answer are never rendered', async () => {
  const html = await render('<script>alert(1)</script>\n\n<img src=x onerror=alert(1)>\n\n[open](javascript:alert(1))');
  assert.doesNotMatch(html, /<script|<img|href="javascript:/i);
});
