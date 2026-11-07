import test from 'node:test';
import assert from 'node:assert/strict';
const load = async () => {
  try { return await import('../src/stream.mjs'); }
  catch { assert.fail('The SSE stream adapter is not implemented'); }
};
function response(chunks) {
  return new Response(new ReadableStream({start(controller) {
    for (const chunk of chunks) controller.enqueue(new TextEncoder().encode(chunk));
    controller.close();
  }}));
}
test('SSE frames can span network chunks and include multiple frames per chunk', async () => {
  const {readEvents} = await load();
  const events = [];
  for await (const event of readEvents(response(['event: del','ta\ndata: {"text":"hello"}\n', '\nevent: done\ndata: {"tool_calls":1}\n\n']))) events.push(event);
  assert.deepEqual(events.map(e => e.event), ['delta','done']);
  assert.equal(events[0].data.text,'hello');
});
test('A disconnected stream cannot become a completed answer', async () => {
  const {readEvents} = await load();
  await assert.rejects(async () => { for await (const event of readEvents(response(['event: delta\ndata: {"text":"partial"}\n\n']))) {} }, /ended before/);
});
test('An error frame rejects with the safe server message', async () => {
  const {readEvents} = await load();
  await assert.rejects(async () => { for await (const event of readEvents(response(['event: error\ndata: {"message":"Setup pending"}\n\n']))) {} }, /Setup pending/);
});
