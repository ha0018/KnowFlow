export type SSEEvent = { event: string, data: any }

export class SSEParser {
  private buffer = ''
  feed(text: string): SSEEvent[] {
    this.buffer += text
    // Normalize only complete CRLF pairs; a trailing CR may be split across packets.
    this.buffer = this.buffer.replace(/\r\n/g, '\n')
    const events: SSEEvent[] = []
    let end: number
    while ((end = this.buffer.indexOf('\n\n')) >= 0) {
      const block = this.buffer.slice(0, end); this.buffer = this.buffer.slice(end + 2)
      let name = 'message'; const data: string[] = []
      for (const line of block.split('\n')) {
        if (line.startsWith('event:')) name = line.slice(6).trim()
        if (line.startsWith('data:')) data.push(line.slice(5).trimStart())
      }
      if (data.length) events.push({ event: name, data: JSON.parse(data.join('\n')) })
    }
    return events
  }
}

export async function readSSE(response: Response, onEvent: (event: SSEEvent) => void) {
  if (!response.ok || !response.headers.get('content-type')?.includes('text/event-stream')) {
    const data = await response.json()
    throw new Error(data.replayed ? `请求已提交，可通过运行记录查看（${data.status}）` : JSON.stringify(data.error || data))
  }
  const reader = response.body!.getReader(); const decoder = new TextDecoder(); const parser = new SSEParser()
  try {
    while (true) {
      const { value, done } = await reader.read()
      for (const event of parser.feed(decoder.decode(value, { stream: !done }))) onEvent(event)
      if (done) break
    }
  } finally { reader.releaseLock() }
}
