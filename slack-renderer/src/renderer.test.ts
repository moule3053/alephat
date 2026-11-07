import assert from "node:assert/strict"
import { test } from "node:test"
import { SlackRenderer } from "./renderer.ts"

test("Markdown replies render bold and links without altering inline code", () => {
  const renderer = new SlackRenderer()
  const result = renderer.render({
    markdown:
      "I'm **Alephat**.\n\n- **Code**: use `a ** b` or [open PR](https://github.com/acme/repo/pull/1).",
  })
  const output = JSON.stringify(result)
  assert.match(output, /\*Alephat\*/)
  assert.doesNotMatch(output, /\*\*Alephat\*\*/)
  assert.match(output, /`a \*\* b`/)
  assert.match(output, /<https:\/\/github.com\/acme\/repo\/pull\/1\|open PR>/)
})

test("Markdown tables and image-only paragraphs become native blocks", () => {
  const result = new SlackRenderer().render({
    markdown:
      "| Service | Count |\n|---|---:|\n| API | 12 |\n\n![Preview](https://example.com/preview.png)",
  })
  const blocks = result.messages.flatMap((message) => message.blocks)
  assert.ok(blocks.some((block) => block.type === "data_table"))
  assert.ok(blocks.some((block) => block.type === "image"))
})

test("Structured charts, repeated fields, and buttons use the actual json-render runtime", () => {
  const result = new SlackRenderer().render({
    spec: {
      root: "card",
      state: {
        metrics: [
          { label: "Builds", value: 8 },
          { label: "Tests", value: 42 },
        ],
      },
      elements: {
        card: {
          type: "Card",
          props: { title: "Build report" },
          children: ["fields", "chart", "actions"],
        },
        fields: {
          type: "Fields",
          props: {},
          repeat: { statePath: "/metrics" },
          children: ["field"],
        },
        field: {
          type: "Field",
          props: { label: { $item: "label" }, value: { $item: "value" } },
        },
        chart: {
          type: "Chart",
          props: {
            title: "Builds",
            chart: {
              type: "bar",
              categories: ["Mon", "Tue"],
              series: [{ name: "Passed", values: [3, 5] }],
            },
          },
        },
        actions: { type: "Actions", props: {}, children: ["button"] },
        button: {
          type: "Button",
          props: {
            id: "details",
            label: "Explain results",
            value: "Explain the test results",
          },
        },
      },
    },
  })
  const output = JSON.stringify(result)
  assert.match(output, /data_visualization/)
  assert.match(output, /Builds/)
  assert.match(output, /Tests/)
  assert.match(output, /Explain results/)
})

test("Long fenced output is split across valid sections without dropping content", () => {
  const body = Array.from({ length: 3000 }, (_, index) => `line_${index}`).join(
    "\n"
  )
  const result = new SlackRenderer().render({
    markdown: `\`\`\`python\n${body}\n\`\`\``,
  })
  const blocks = result.messages.flatMap((message) => message.blocks)
  for (const block of blocks) {
    const output = JSON.stringify(block)
    assert.ok(output.length < 4000)
  }
  const output = JSON.stringify(blocks)
  assert.match(output, /line_0\\n/)
  assert.match(output, /line_2999/)
  assert.doesNotMatch(output, /…/)
})

test("Unsafe image and navigation schemes are rejected before Slack delivery", () => {
  assert.throws(
    () =>
      new SlackRenderer().render({
        markdown: "![Private](http://localhost/image.png)",
      }),
    /HTTPS/
  )
})

test("Oversized tables retain the final row instead of the SDK's truncated ASCII fallback", () => {
  const result = new SlackRenderer().render({
    spec: {
      root: "card",
      elements: {
        card: { type: "Card", props: {}, children: ["table"] },
        table: {
          type: "Table",
          props: {
            headers: ["Item", "Description"],
            rows: Array.from({ length: 150 }, (_, index) => [
              `row_${index}`,
              "Complete report data".repeat(5),
            ]),
          },
        },
      },
    },
  })
  const output = JSON.stringify(
    result.messages.flatMap((message) => message.blocks)
  )
  assert.match(output, /row_149/)
  assert.doesNotMatch(output, /…/)
})
