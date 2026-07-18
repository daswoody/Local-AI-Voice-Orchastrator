<script lang="ts">
  // Rekursiver Renderer fuer das plattformneutrale Layout-JSON (CARDS.md).
  // Android rendert dieselben Templates mit Compose - hier ist die
  // Web-Entsprechung fuer Windows-Shell UND Browser.
  import type { CardEnvelope } from "../lib/api";
  import { ICONS, resolveBindings, resolvePath } from "../lib/cards";
  import CardView from "./CardView.svelte";

  let {
    node,
    envelope,
    scope = undefined,
    ontool = undefined,
  }: {
    node: Record<string, any>;
    envelope: CardEnvelope;
    scope?: unknown;
    ontool?: (tool: string, args: Record<string, unknown>) => void;
  } = $props();

  const text = (template: string | undefined) =>
    template ? resolveBindings(template, envelope, scope) : "";

  function runAction() {
    const action = node.action;
    if (!action) return;
    if (action.type === "open_url") {
      const url = text(action.url);
      if (url) window.open(url, "_blank");
    } else if (action.type === "device_tool") {
      ontool?.(action.tool, action.arguments ?? {});
    }
  }

  const listItems = $derived.by(() => {
    if (node.component !== "list") return [];
    const items = resolvePath(node.items_path ?? "data.items", envelope, scope);
    return Array.isArray(items) ? items : [];
  });
</script>

{#if node.component === "column"}
  <div class="col" style:align-items={node.align === "center" ? "center" : node.align === "end" ? "flex-end" : "stretch"} style:padding={node.padding ? `${node.padding}px` : undefined}>
    {#each node.children ?? [] as child}
      <CardView node={child} {envelope} {scope} {ontool} />
    {/each}
  </div>
{:else if node.component === "row"}
  <div class="row" style:justify-content={node.align === "space_between" ? "space-between" : node.align === "center" ? "center" : node.align === "end" ? "flex-end" : "flex-start"} style:padding={node.padding ? `${node.padding}px` : undefined}>
    {#each node.children ?? [] as child}
      <div style:flex={child.weight ? `${child.weight} 1 0` : undefined}>
        <CardView node={child} {envelope} {scope} {ontool} />
      </div>
    {/each}
  </div>
{:else if node.component === "text"}
  <span class="text {node.style ?? 'body'}" style:color={node.color?.startsWith("#") ? node.color : undefined} class:c-primary={node.color === "primary"} class:c-secondary={node.color === "secondary"} class:c-muted={node.color === "muted"}>{text(node.text)}</span>
{:else if node.component === "image"}
  {#if text(node.url ?? node.src)}
    <img src={text(node.url ?? node.src)} alt="" style:max-width="100%" style:height={node.size ? `${node.size}px` : undefined} />
  {/if}
{:else if node.component === "icon"}
  <span style:font-size={node.size ? `${node.size}px` : "20px"}>{ICONS[node.icon] ?? "•"}</span>
{:else if node.component === "divider"}
  <hr />
{:else if node.component === "spacer"}
  <div style:height={`${node.size ?? 8}px`} style:width={`${node.size ?? 8}px`}></div>
{:else if node.component === "badge"}
  <span class="badge">{text(node.text)}</span>
{:else if node.component === "progress"}
  {@const value = Number(text(String(node.value ?? 0))) || 0}
  <div class="progress"><div class="bar" style:width={`${Math.min(100, Math.max(0, value * 100))}%`}></div></div>
{:else if node.component === "button"}
  <button class="card-btn" onclick={runAction}>{text(node.label ?? node.action?.label ?? "Aktion")}</button>
{:else if node.component === "list"}
  <div class="col">
    {#each listItems as item}
      <CardView node={node.item_template} {envelope} scope={item} {ontool} />
    {/each}
  </div>
{/if}

<style>
  .col {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .row {
    display: flex;
    flex-direction: row;
    align-items: center;
    gap: 8px;
  }
  .text {
    line-height: 1.4;
  }
  .text.display {
    font-size: 34px;
    font-weight: 700;
  }
  .text.title {
    font-size: 17px;
    font-weight: 600;
  }
  .text.label {
    font-size: 12px;
    color: var(--text-muted);
  }
  .c-primary {
    color: var(--primary);
  }
  .c-secondary {
    color: var(--ok);
  }
  .c-muted {
    color: var(--text-muted);
  }
  hr {
    border: none;
    border-top: 1px solid var(--border);
    margin: 6px 0;
    width: 100%;
  }
  .badge {
    background: var(--primary-dim);
    border-radius: 999px;
    padding: 2px 10px;
    font-size: 12px;
  }
  .progress {
    background: var(--bg-input);
    border-radius: 999px;
    height: 8px;
    overflow: hidden;
    width: 100%;
  }
  .bar {
    background: var(--primary);
    height: 100%;
  }
  .card-btn {
    background: var(--primary);
    color: #0b1016;
    border: none;
    font-weight: 600;
    padding: 7px 12px;
  }
</style>
