import { describe, expect, it } from 'vitest'
import { SSEParser } from './sse'

describe('SSE stream framing', () => {
  it('handles UTF-8 byte boundaries, split CRLF, heartbeats and merged events', () => {
    const wire = ': heartbeat\r\n\r\nevent: delta\r\ndata: {"text":"退款政策"}\r\n\r\nevent: done\ndata: {"status":"completed"}\n\n'
    const bytes = new TextEncoder().encode(wire), decoder = new TextDecoder(), parser = new SSEParser()
    const events = []
    for (const byte of bytes) events.push(...parser.feed(decoder.decode(new Uint8Array([byte]), { stream: true })))
    events.push(...parser.feed(decoder.decode()))
    expect(events).toEqual([{ event: 'delta', data: { text: '退款政策' } }, { event: 'done', data: { status: 'completed' } }])
  })
  it('keeps incomplete events until their separator arrives', () => {
    const parser = new SSEParser()
    expect(parser.feed('event: meta\ndata: {"run_id":')).toEqual([])
    expect(parser.feed('"abc"}\n\n')).toEqual([{ event: 'meta', data: { run_id: 'abc' } }])
  })
})
