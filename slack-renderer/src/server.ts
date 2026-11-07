import {
  createServer,
  type IncomingMessage,
  type ServerResponse,
} from "node:http"
import { timingSafeEqual } from "node:crypto"
import { SlackRenderer } from "./renderer.ts"

class RendererServer {
  readonly renderer = new SlackRenderer()
  readonly token = process.env.SLACK_RENDERER_TOKEN ?? ""

  async handle(
    request: IncomingMessage,
    response: ServerResponse
  ): Promise<void> {
    response.setHeader("Content-Type", "application/json")
    response.setHeader("Cache-Control", "no-store")
    if (request.url === "/healthz")
      return this.send(response, 200, { status: "ok" })
    const authorization = Buffer.from(request.headers.authorization ?? "")
    const expected = Buffer.from(`Bearer ${this.token}`)
    if (
      this.token &&
      (authorization.length !== expected.length ||
        !timingSafeEqual(authorization, expected))
    ) {
      return this.send(response, 401, { error: "Unauthorized" })
    }
    try {
      if (request.method === "GET" && request.url === "/catalog") {
        return this.send(response, 200, this.renderer.describe())
      }
      if (request.method !== "POST" || request.url !== "/render") {
        return this.send(response, 404, { error: "Not found" })
      }
      const chunks: Buffer[] = []
      let length = 0
      for await (const chunk of request) {
        const bytes: Buffer = Buffer.isBuffer(chunk)
          ? chunk
          : Buffer.from(chunk)
        length += bytes.length
        if (length > 2_000_000)
          return this.send(response, 413, { error: "Message exceeds 2 MB" })
        chunks.push(bytes)
      }
      const body: unknown = JSON.parse(Buffer.concat(chunks).toString("utf8"))
      this.send(response, 200, this.renderer.render(body))
    } catch (error) {
      // Validation details go back to the caller, never into server logs.
      this.send(response, 422, {
        error: error instanceof Error ? error.message : "Invalid message",
      })
    }
  }

  private send(response: ServerResponse, status: number, body: unknown): void {
    response.writeHead(status)
    response.end(JSON.stringify(body))
  }

  start(): void {
    const server = createServer(
      (request, response) => void this.handle(request, response)
    )
    server.requestTimeout = 15_000
    server.listen(
      Number(process.env.PORT ?? 8082),
      process.env.HOST ?? "127.0.0.1"
    )
  }
}

new RendererServer().start()
