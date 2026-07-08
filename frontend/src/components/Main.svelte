<script lang="ts">
  // Hauptansicht: Sidebar (neuer Chat, zentrale Verlaufs-Liste vom Server,
  // Einstellungen) + Inhalt (Chat oder Einstellungen).
  import { onMount } from "svelte";
  import {
    deleteConversation,
    getConversation,
    listConversations,
    logout,
    storedUser,
    type ConversationSummary,
  } from "../lib/api";
  import { syncLayouts } from "../lib/cards";
  import { app, closeSession, initShellIntegration, loadIntoChat, newChat } from "../lib/session.svelte";
  import Chat from "./Chat.svelte";
  import Settings from "./Settings.svelte";

  let { onlogout }: { onlogout: () => void } = $props();

  let view: "chat" | "settings" = $state("chat");
  let conversations: ConversationSummary[] = $state([]);
  const user = storedUser();

  async function refreshConversations() {
    try {
      conversations = (await listConversations()).conversations;
    } catch {
      /* Historie ist Komfort - Fehler nicht in den Chat drücken */
    }
  }

  onMount(() => {
    void syncLayouts();
    void refreshConversations();
    initShellIntegration();
  });

  // Nach jedem abgeschlossenen Turn taucht das Gespräch (neu) in der Liste auf.
  $effect(() => {
    void app.conversationId;
    void refreshConversations();
  });

  async function openConversation(id: string) {
    const detail = await getConversation(id);
    loadIntoChat(id, detail.messages);
    view = "chat";
  }

  async function removeConversation(event: MouseEvent, id: string) {
    event.stopPropagation();
    await deleteConversation(id);
    if (app.conversationId === id) newChat();
    await refreshConversations();
  }

  function startNewChat() {
    newChat();
    view = "chat";
  }

  function doLogout() {
    closeSession();
    logout();
    onlogout();
  }
</script>

<div class="layout">
  <aside>
    <div class="brand">🏠 Heim-AI</div>
    <button class="primary new-chat" onclick={startNewChat}>＋ Neuer Chat</button>

    <div class="list">
      {#each conversations as conversation (conversation.id)}
        <div
          class="item"
          class:active={conversation.id === app.conversationId}
          role="button"
          tabindex="0"
          onclick={() => void openConversation(conversation.id)}
          onkeydown={(e) => e.key === "Enter" && void openConversation(conversation.id)}
        >
          <span class="item-title">{conversation.title || "Ohne Titel"}</span>
          <button
            class="ghost del"
            title="Gespräch löschen"
            onclick={(e) => void removeConversation(e, conversation.id)}
          >🗑</button>
        </div>
      {/each}
    </div>

    <div class="footer">
      <div class="user">
        {user?.name}
        <span class="tier">Tier {user?.tier}</span>
      </div>
      <button class="ghost" onclick={() => (view = view === "settings" ? "chat" : "settings")}>
        {view === "settings" ? "← Zurück" : "⚙️ Einstellungen"}
      </button>
      <button class="ghost" onclick={doLogout}>Abmelden</button>
    </div>
  </aside>

  <main>
    {#if view === "settings"}
      <Settings />
    {:else}
      <Chat />
    {/if}
  </main>
</div>

<style>
  .layout {
    display: flex;
    height: 100%;
  }
  aside {
    width: 260px;
    background: var(--bg-raised);
    border-right: 1px solid var(--border);
    display: flex;
    flex-direction: column;
    padding: 14px;
    gap: 12px;
  }
  .brand {
    font-weight: 700;
    font-size: 17px;
    padding: 4px 2px;
  }
  .new-chat {
    width: 100%;
  }
  .list {
    flex: 1;
    overflow-y: auto;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .item {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 8px 10px;
    border-radius: 8px;
    cursor: pointer;
    color: var(--text-muted);
  }
  .item:hover {
    background: var(--bg-input);
    color: var(--text);
  }
  .item.active {
    background: var(--bg-input);
    color: var(--text);
  }
  .item-title {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-size: 14px;
  }
  .del {
    padding: 0 4px;
    font-size: 13px;
    visibility: hidden;
  }
  .item:hover .del {
    visibility: visible;
  }
  .footer {
    border-top: 1px solid var(--border);
    padding-top: 10px;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .footer .ghost {
    text-align: left;
    padding: 6px 8px;
  }
  .user {
    padding: 2px 8px;
    font-weight: 600;
  }
  .tier {
    font-weight: 400;
    color: var(--text-muted);
    font-size: 12px;
    margin-left: 6px;
  }
  main {
    flex: 1;
    min-width: 0;
  }
</style>
