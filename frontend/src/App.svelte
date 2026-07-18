<script lang="ts">
  // Einstieg + Mini-Routing ueber den URL-Hash. Die Shell oeffnet fuer
  // Indikator und Popups eigene Fenster auf dieselbe UI mit #/indicator
  // bzw. #/popup - so gibt es genau EINEN Karten-Renderer (4.12).
  import { storedUser } from "./lib/api";
  import Indicator from "./components/Indicator.svelte";
  import Login from "./components/Login.svelte";
  import Main from "./components/Main.svelte";
  import Popup from "./components/Popup.svelte";

  let route = $state(location.hash);
  let loggedIn = $state(storedUser() !== null);

  window.addEventListener("hashchange", () => (route = location.hash));
  window.addEventListener("heimai:logout", () => (loggedIn = false));

  // Transparenter Fenstergrund NUR fuer die Overlay-Fenster der Shell.
  $effect(() => {
    const overlay = route.startsWith("#/indicator") || route.startsWith("#/popup");
    document.body.classList.toggle("overlay-window", overlay);
  });
</script>

{#if route.startsWith("#/indicator")}
  <Indicator />
{:else if route.startsWith("#/popup")}
  <Popup />
{:else if !loggedIn}
  <Login onlogin={() => (loggedIn = true)} />
{:else}
  <Main onlogout={() => (loggedIn = false)} />
{/if}
