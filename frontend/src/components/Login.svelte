<script lang="ts">
  import { login } from "../lib/api";
  import { inShell } from "../lib/shell";

  let { onlogin }: { onlogin: () => void } = $props();

  let username = $state("");
  let password = $state("");
  let error = $state("");
  let busy = $state(false);

  async function submit(event: SubmitEvent) {
    event.preventDefault();
    busy = true;
    error = "";
    try {
      await login(username, password, inShell ? "Windows-PC" : "Browser");
      onlogin();
    } catch (e) {
      error = e instanceof Error ? e.message : "Login fehlgeschlagen";
    } finally {
      busy = false;
    }
  }
</script>

<div class="wrap">
  <form onsubmit={submit}>
    <div class="logo">🏠</div>
    <h1>Heim-AI</h1>
    <p class="sub">Anmelden am Voice-Orchestrator</p>

    <label for="username">Benutzername</label>
    <input id="username" bind:value={username} autocomplete="username" required />

    <label for="password">Passwort</label>
    <input id="password" type="password" bind:value={password} autocomplete="current-password" required />

    {#if error}<p class="error">{error}</p>{/if}

    <button class="primary" type="submit" disabled={busy}>
      {busy ? "Anmelden…" : "Anmelden"}
    </button>
  </form>
</div>

<style>
  .wrap {
    height: 100%;
    display: grid;
    place-items: center;
  }
  form {
    width: min(340px, 90vw);
    background: var(--bg-raised);
    border: 1px solid var(--border);
    border-radius: 16px;
    padding: 28px;
    text-align: center;
  }
  .logo {
    font-size: 42px;
  }
  h1 {
    margin: 4px 0 0;
    font-size: 22px;
  }
  .sub {
    color: var(--text-muted);
    margin: 4px 0 12px;
    font-size: 13px;
  }
  label {
    text-align: left;
  }
  .error {
    color: var(--danger);
    font-size: 13px;
  }
  button {
    width: 100%;
    margin-top: 18px;
  }
</style>
