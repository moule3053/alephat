import {
  cardToBlockKit,
  cardToFallbackText,
  SlackMarkdownConverter,
} from "@chat-adapter/slack"
import { defineCatalog, type Spec } from "@json-render/core"
import {
  schema,
  standardComponentDefinitions,
  toCard,
  toPartialCard,
} from "@json-render/chat"
import {
  parseMarkdown,
  stringifyMarkdown,
  tableElementToAscii,
  chartElementToFallbackText,
  type CardElement,
  type CardChild,
} from "chat"
import { z } from "zod"

const element = z
  .object({
    type: z.string(),
    props: z.record(z.string(), z.unknown()).default({}),
    children: z.array(z.string()).default([]),
  })
  .passthrough()

const requestSchema = z
  .object({
    markdown: z.string().max(200_000).optional(),
    spec: z
      .object({
        root: z.string(),
        elements: z.record(z.string(), element),
        state: z.record(z.string(), z.unknown()).optional(),
      })
      .optional(),
    partial: z.boolean().default(false),
  })
  .refine(
    (value) => (value.markdown !== undefined) !== (value.spec !== undefined),
    {
      message: "Supply either markdown or spec",
    }
  )

type Block = ReturnType<typeof cardToBlockKit>[number]
export interface RenderedMessage {
  text: string
  blocks: Block[]
}

export class SlackRenderer {
  readonly catalog = defineCatalog(schema, {
    components: standardComponentDefinitions,
  })
  readonly markdown = new SlackMarkdownConverter()

  describe() {
    return { prompt: this.catalog.prompt(), schema: this.catalog.jsonSchema() }
  }

  render(input: unknown): { messages: RenderedMessage[] } {
    const request = requestSchema.parse(input)
    let card: CardElement | null
    if (request.spec) {
      // The core Spec permits dynamic props, visibility, state, and repeat.
      const spec = request.spec as Spec
      card = request.partial
        ? toPartialCard(spec, { maxElements: 1000 })
        : toCard(spec, { maxElements: 1000 })
    } else {
      card = this.fromMarkdown(request.markdown ?? "")
    }
    if (!card) return { messages: [] }
    this.prepare(card)
    const blocks = cardToBlockKit(card)
    const messages: RenderedMessage[] = []
    for (let index = 0; index < blocks.length; index += 50) {
      const part = blocks.slice(index, index + 50)
      messages.push({
        text: cardToFallbackText(card).slice(0, 39000),
        blocks: part,
      })
    }
    return { messages }
  }

  private fromMarkdown(markdown: string): CardElement {
    const ast = parseMarkdown(markdown.trim() || "Task completed.")
    const children: CardElement["children"] = []
    for (const node of ast.children) {
      if (node.type === "table") {
        const rows = node.children.map((row) =>
          row.children.map((cell) =>
            stringifyMarkdown({
              type: "root",
              children: [{ type: "paragraph", children: cell.children }],
            }).trim()
          )
        )
        children.push({
          type: "table",
          headers: rows[0] ?? [],
          rows: rows.slice(1),
          caption: "Results",
        })
      } else if (node.type === "thematicBreak") {
        children.push({ type: "divider" })
      } else if (
        node.type === "paragraph" &&
        node.children.some((child) => child.type === "image")
      ) {
        let pending: typeof node.children = []
        for (const child of node.children) {
          if (child.type === "image") {
            if (pending.length)
              children.push({
                type: "text",
                content: stringifyMarkdown({
                  type: "root",
                  children: [{ type: "paragraph", children: pending }],
                }).trim(),
              })
            pending = []
            children.push({
              type: "image",
              url: child.url,
              alt: child.alt || "Image",
            })
          } else pending.push(child)
        }
        if (pending.length)
          children.push({
            type: "text",
            content: stringifyMarkdown({
              type: "root",
              children: [{ type: "paragraph", children: pending }],
            }).trim(),
          })
      } else {
        const content =
          node.type === "heading"
            ? `**${stringifyMarkdown({ type: "root", children: [{ type: "paragraph", children: node.children }] }).trim()}**`
            : stringifyMarkdown({ type: "root", children: [node] }).trim()
        children.push({ type: "text", content })
      }
    }
    return { type: "card", children }
  }

  private prepare(card: CardElement): void {
    if (card.imageUrl) this.safeUrl(card.imageUrl)
    if (card.title && card.title.length > 150) {
      card.children.unshift({
        type: "text",
        content: card.title,
        style: "bold",
      })
      delete card.title
    }
    if (card.subtitle && card.subtitle.length > 2800) {
      card.children.unshift({
        type: "text",
        content: card.subtitle,
        style: "muted",
      })
      delete card.subtitle
    }
    card.children = this.prepareChildren(card.children)
  }

  private prepareChildren(
    children: CardElement["children"],
    usage = { tables: 0, charts: 0 }
  ): CardElement["children"] {
    return children.flatMap<CardChild>((child) => {
      if (child.type === "section") {
        child.children = this.prepareChildren(child.children, usage)
      } else if (child.type === "table" || child.type === "chart") {
        const native = cardToBlockKit({ type: "card", children: [child] })[0]
        const supported =
          child.type === "table"
            ? usage.tables < 1 &&
              ["data_table", "table"].includes(native?.type ?? "")
            : usage.charts < 2 && native?.type === "data_visualization"
        if (supported) {
          if (child.type === "table") usage.tables++
          else usage.charts++
        } else {
          // The SDK's ASCII fallback truncates at 3,000 characters. Split ours
          // first so overflowing tables/charts retain their complete data.
          const data =
            child.type === "table"
              ? tableElementToAscii(child.headers, child.rows)
              : chartElementToFallbackText(child)
          return this.prepareChildren(
            [{ type: "text", content: `\`\`\`\n${data}\n\`\`\`` }],
            usage
          )
        }
      } else if (child.type === "image") {
        this.safeUrl(child.url)
      } else if (child.type === "actions") {
        if (child.children.length > 25)
          throw new Error("At most 25 actions per group")
        for (const button of child.children) {
          if (button.type === "link-button") this.safeUrl(button.url)
          else if (
            button.type === "button" &&
            (button.id.length > 100 || (button.value?.length ?? 0) > 500)
          ) {
            throw new Error("Button id or value is too long")
          }
        }
      } else if (child.type === "fields") {
        if (child.children.length > 10)
          throw new Error("At most 10 fields per group")
        for (const field of child.children) {
          field.label = this.markdown.toResponseUrlText({
            markdown: field.label,
          })
          field.value = this.markdown.toResponseUrlText({
            markdown: field.value,
          })
          if (field.label.length + field.value.length > 1990)
            throw new Error(
              "Field content is too long; use a Text component instead"
            )
        }
      } else if (child.type === "text") {
        // Parse Markdown, not a regex replacement, so code and links survive.
        const content = this.markdown.toResponseUrlText({
          markdown: child.content,
        })
        return this.splitText(content).map((part) => ({
          ...child,
          content: part,
        }))
      }
      return [child]
    })
  }

  private splitText(text: string): string[] {
    const chunks: string[] = []
    let remaining = text
    let reopen = false
    while (remaining.length) {
      const prefix = reopen ? "```\n" : ""
      const limit = 2800 - prefix.length
      let end = Math.min(remaining.length, limit)
      if (end < remaining.length) {
        const newline = remaining.lastIndexOf("\n", end)
        if (newline > limit / 2) end = newline + 1
        const code = remaining.charCodeAt(end - 1)
        if (code >= 0xd800 && code <= 0xdbff) end--
      }
      const part = remaining.slice(0, end)
      const openFence: boolean =
        ((part.match(/```/g)?.length ?? 0) + Number(reopen)) % 2 === 1
      chunks.push(prefix + part + (openFence ? "\n```" : ""))
      reopen = openFence
      remaining = remaining.slice(end)
    }
    return chunks
  }

  private safeUrl(value: string): void {
    const url = new URL(value)
    if (url.protocol !== "https:" || url.username || url.password) {
      throw new Error(
        "Images and links require an HTTPS URL without credentials; upload private files instead"
      )
    }
  }
}
