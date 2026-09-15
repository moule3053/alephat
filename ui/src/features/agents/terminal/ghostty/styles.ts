export function installGhosttyStyles(mount: HTMLElement): void {
  mount.style.position = "relative"
  const style = document.createElement("style")
  style.textContent = `
.alephat-ghostty-canvas{cursor:text}
.alephat-ghostty-scrollbar{position:absolute;z-index:1;top:4px;right:1px;bottom:4px;width:8px;cursor:default;touch-action:none}
.alephat-ghostty-scrollbar-thumb{position:absolute;top:0;right:1px;left:1px;border-radius:3px;background:rgba(148,163,184,.45);transition:background-color 120ms ease-out}
.alephat-ghostty-scrollbar:hover .alephat-ghostty-scrollbar-thumb,.alephat-ghostty-scrollbar:focus-visible .alephat-ghostty-scrollbar-thumb{background:rgba(148,163,184,.75)}
`
  mount.append(style)
}
