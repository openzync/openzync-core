/**
 * SSE stream reader for the OpenZep chat endpoint.
 *
 * Usage:
 * ```ts
 * const stream = readChatSSE(
 *   "http://localhost:8000/v1/users/..." ,
 *   { session_id: "...", message: "Hello" },
 *   "bearer-token"
 * );
 * for await (const event of stream) {
 *   console.log(event.type, event);
 * }
 * ```
 */

export interface ChatEvent {
  type: "message_stored" | "tool_call" | "tool_result" | "tool_calls_start" | "tool_calls_end" | "start" | "token" | "error" | "done";
  [key: string]: unknown;
}

export async function* readChatSSE(
  url: string,
  body: { session_id?: string | null; message: string },
  token: string
): AsyncGenerator<ChatEvent> {
  const response = await fetch(url, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
      Accept: "text/event-stream",
    },
    body: JSON.stringify(body),
  });

  if (!response.ok) {
    const text = await response.text().catch(() => "Unknown error");
    throw new Error(`Chat SSE request failed: ${response.status} ${text}`);
  }

  const reader = response.body?.getReader();
  if (!reader) throw new Error("Response body is not readable");

  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });

    // Split on double newlines (SSE event boundary)
    const parts = buffer.split("\n\n");
    buffer = parts.pop() ?? "";

    for (const part of parts) {
      const trimmed = part.trim();
      if (!trimmed) continue;

      // Each SSE event: "data: {json}\n"
      for (const line of trimmed.split("\n")) {
        if (line.startsWith("data: ")) {
          try {
            const data = JSON.parse(line.slice(6)) as ChatEvent;
            yield data;
          } catch {
            // Ignore malformed JSON events
            console.warn("Failed to parse SSE event:", line);
          }
        }
      }
    }
  }
}
