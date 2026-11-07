/** Parse the backend's SSE protocol without assuming network chunk boundaries. */
export async function* readEvents(response) {
  if (!response.body) throw new Error('No response stream was received.');
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let complete = false;
  try {
    for (;;) {
      const {value, done} = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, {stream:true});
      if (buffer.length > 1000000) throw new Error('Response exceeded the supported size.');
      let boundary;
      while ((boundary = buffer.indexOf('\n\n')) !== -1) {
        const lines = buffer.slice(0,boundary).split('\n');
        buffer = buffer.slice(boundary + 2);
        const event = lines.find(line => line.startsWith('event: '))?.slice(7);
        const raw = lines.filter(line => line.startsWith('data: ')).map(line => line.slice(6)).join('\n');
        if (!event || !raw) continue;
        const data = JSON.parse(raw);
        if (event === 'error') throw new Error(data.message || 'The answer could not be completed.');
        yield {event,data};
        if (event === 'done') complete = true;
      }
    }
    if (!complete) throw new Error('The connection ended before the answer completed. Try again.');
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}
