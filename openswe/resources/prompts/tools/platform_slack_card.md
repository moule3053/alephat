Render a validated json-render chat specification into the current Slack reply.
`spec_json` is a JSON string with `root`, `elements`, and optional `state`, using the provided chat catalog.
Supports cards, sections, text, fields, tables, charts, images, dividers, buttons, state bindings, visibility conditions, and repeats.
`fallback_text` must describe the complete card for accessibility and delivery failures.
Calling again updates the same reply.
Returns posted status or a validation error to correct.
Use ordinary Markdown for simple answers.
