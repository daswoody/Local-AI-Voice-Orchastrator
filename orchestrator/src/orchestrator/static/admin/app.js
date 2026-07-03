/* Heim-AI Admin-Panel (Mikro-Phase 1.7c)
 *
 * Bewusst Vanilla-JS ohne Build-Step (Entscheidung 4.14): kein Node-Toolchain,
 * direkt von FastAPI ausgeliefert, fuer den Umfang eines Admin-Panels voellig
 * ausreichend. Hash-Routing (#/users usw.), Token in localStorage.
 */

"use strict";

// ---- API-Helfer ---------------------------------------------------------------

const api = {
  token: localStorage.getItem("heimai_token") || null,

  async request(method, path, body, isForm = false) {
    const headers = {};
    if (this.token) headers["Authorization"] = `Bearer ${this.token}`;
    if (body && !isForm) headers["Content-Type"] = "application/json";

    const response = await fetch(path, {
      method,
      headers,
      body: body ? (isForm ? body : JSON.stringify(body)) : undefined,
    });

    if (response.status === 401) {
      logout();
      throw new Error("Sitzung abgelaufen - bitte neu anmelden.");
    }
    if (!response.ok) {
      let detail = `${response.status}`;
      try { detail = (await response.json()).detail || detail; } catch (_) {}
      throw new Error(detail);
    }
    if (response.status === 204) return null;
    return response.json();
  },

  get(path) { return this.request("GET", path); },
  post(path, body) { return this.request("POST", path, body); },
  put(path, body) { return this.request("PUT", path, body); },
  del(path) { return this.request("DELETE", path); },
};

// ---- Login / Logout -------------------------------------------------------------

function logout() {
  localStorage.removeItem("heimai_token");
  api.token = null;
  document.getElementById("app").classList.add("hidden");
  document.getElementById("login-screen").classList.remove("hidden");
}

async function handleLogin(event) {
  event.preventDefault();
  const form = event.target;
  const errorBox = document.getElementById("login-error");
  errorBox.classList.add("hidden");
  try {
    const data = await api.request("POST", "/v1/auth/login", {
      username: form.username.value,
      password: form.password.value,
      device_name: "admin-panel",
    });
    if (data.user.tier < 3) {
      throw new Error("Nur Tier-3-Konten (Admin) haben Zugriff.");
    }
    api.token = data.token;
    localStorage.setItem("heimai_token", data.token);
    showApp();
  } catch (err) {
    errorBox.textContent = err.message;
    errorBox.classList.remove("hidden");
  }
}

function showApp() {
  document.getElementById("login-screen").classList.add("hidden");
  document.getElementById("app").classList.remove("hidden");
  render();
}

// ---- Routing ---------------------------------------------------------------------

const views = {};

function currentView() {
  return (location.hash.replace("#/", "") || "models").split("?")[0];
}

async function render() {
  const name = currentView();
  document.querySelectorAll("#sidebar a").forEach((a) =>
    a.classList.toggle("active", a.dataset.view === name)
  );
  const container = document.getElementById("content");
  container.innerHTML = "<p class='hint'>Laedt…</p>";
  try {
    container.innerHTML = await (views[name] || views.models)();
    wireForms(container);
  } catch (err) {
    container.innerHTML = `<div class="notice error">${esc(err.message)}</div>`;
  }
}

function esc(value) {
  const div = document.createElement("div");
  div.textContent = String(value ?? "");
  return div.innerHTML;
}

/* Formulare deklarativ verdrahten: data-action-Attribute statt onclick,
 * damit kein HTML-escaping-Chaos mit Nutzerdaten entsteht. */
function wireForms(root) {
  root.querySelectorAll("[data-submit]").forEach((form) => {
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      try {
        await formActions[form.dataset.submit](form);
        render();
      } catch (err) {
        alert(err.message);
      }
    });
  });
  root.querySelectorAll("[data-action]").forEach((button) => {
    button.addEventListener("click", async () => {
      try {
        await buttonActions[button.dataset.action](button.dataset);
        // Formular-Befueller nicht neu rendern - das wuerde die gerade
        // gesetzten Feldwerte sofort wieder verwerfen.
        if (!NO_RERENDER.has(button.dataset.action)) render();
      } catch (err) {
        alert(err.message);
      }
    });
  });
}

const NO_RERENDER = new Set(["editUser", "resetUserForm", "pickSample", "editCard"]);

// ---- View: Modelle -----------------------------------------------------------------

views.models = async () => {
  let rows = "";
  let notice = "";
  try {
    const data = await api.get("/v1/admin/models");
    rows = data.models.map((model) => {
      const id = model.id || "";
      const active = id === data.active_model;
      return `<tr>
        <td>${esc(id)}</td>
        <td>${active ? '<span class="badge ok">aktiv</span>' : '<span class="badge off">inaktiv</span>'}</td>
        <td class="actions">
          ${active ? "" : `<button class="small" data-action="activateModel" data-id="${esc(id)}">Aktivieren</button>`}
        </td>
      </tr>`;
    }).join("");
  } catch (err) {
    notice = `<div class="notice error">LiteLLM nicht erreichbar: ${esc(err.message)}</div>`;
  }
  return `
    <h1>Modelle</h1>
    <p class="hint">Liste aus LiteLLM - dem einzigen LLM-Zugang (Architektur 4.6). "Aktivieren" setzt das Modell fuer alle Antworten; LM Studio laedt es beim ersten Request selbst (JIT) und entlaedt ungenutzte Modelle per Idle-TTL. Neue Modelle zuerst in LiteLLM registrieren, dann erscheinen sie hier. VRAM-Budget (4.2) beachten.</p>
    ${notice}
    <section class="block">
      <table>
        <thead><tr><th>Modell</th><th>Status</th><th></th></tr></thead>
        <tbody>${rows || "<tr><td colspan='3'>Keine Modelle gefunden.</td></tr>"}</tbody>
      </table>
    </section>`;
};

// ---- View: Charakter ------------------------------------------------------------------

views.character = async () => {
  const data = await api.get("/v1/admin/character");
  return `
    <h1>Charakter</h1>
    <p class="hint">Globaler System-Prompt der Assistenz. Pro Nutzer ueberschreibbar (Feld "Charakter-Override" im Nutzer-Formular).</p>
    <section class="block">
      <form class="grid" data-submit="saveCharacter">
        <label class="full">System-Prompt
          <textarea name="prompt" rows="8">${esc(data.prompt)}</textarea>
        </label>
        <div><button type="submit">Speichern</button></div>
      </form>
    </section>`;
};

// ---- View: Nutzer ---------------------------------------------------------------------

views.users = async () => {
  const [users, voices] = await Promise.all([
    api.get("/v1/admin/users"),
    api.get("/v1/admin/voices"),
  ]);
  const voiceOptions = (selected) =>
    `<option value="">(Standard)</option>` +
    voices.map((v) => `<option value="${esc(v.id)}" ${v.id === selected ? "selected" : ""}>${esc(v.name)}</option>`).join("");

  const rows = users.map((user) => `
    <tr>
      <td>${esc(user.username)}<br><small>${esc(user.display_name)}</small></td>
      <td>Tier ${user.tier}</td>
      <td>${esc(user.default_voice_id || "(Standard)")}</td>
      <td>${user.system_prompt_override ? '<span class="badge ok">Override</span>' : '<span class="badge off">global</span>'}</td>
      <td class="actions">
        <button class="small ghost" data-action="editUser" data-id="${user.id}">Bearbeiten</button>
        <button class="small danger" data-action="deleteUser" data-id="${user.id}" data-name="${esc(user.username)}">Loeschen</button>
      </td>
    </tr>`).join("");

  return `
    <h1>Nutzer</h1>
    <p class="hint">Tier-Modell nach 4.4: 1 = Gast, 2 = User, 3 = Admin (Zugriff auf dieses Panel).</p>
    <section class="block">
      <table>
        <thead><tr><th>Nutzer</th><th>Tier</th><th>Stimme</th><th>Charakter</th><th></th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </section>
    <section class="block">
      <h2 id="user-form-title">Neuen Nutzer anlegen</h2>
      <form class="grid" data-submit="saveUser" id="user-form">
        <input type="hidden" name="id">
        <label>Benutzername <input name="username" required></label>
        <label>Anzeigename <input name="display_name"></label>
        <label>Passwort <input name="password" type="password" placeholder="leer = unveraendert"></label>
        <label>Tier
          <select name="tier">
            <option value="1">1 - Gast</option>
            <option value="2">2 - User</option>
            <option value="3">3 - Admin</option>
          </select>
        </label>
        <label>Standard-Stimme <select name="default_voice_id">${voiceOptions("")}</select></label>
        <label class="full">Charakter-Override (leer = globaler Charakter)
          <textarea name="system_prompt_override" rows="3"></textarea>
        </label>
        <div><button type="submit">Speichern</button>
        <button type="button" class="ghost" data-action="resetUserForm">Neu</button></div>
      </form>
    </section>
    <script type="application/json" id="users-data">${JSON.stringify(users)}</script>`;
};

// ---- View: Stimmen ----------------------------------------------------------------------

views.voices = async () => {
  const voices = await api.get("/v1/admin/voices");
  const rows = voices.map((voice) => `
    <tr>
      <td>${esc(voice.id)}</td>
      <td>${esc(voice.name)}</td>
      <td>${esc(voice.language)}</td>
      <td>${voice.has_sample ? '<span class="badge ok">Sample vorhanden</span>' : '<span class="badge warn">kein Sample</span>'}</td>
      <td class="actions">
        <label style="display:inline-block">
          <input type="file" accept=".wav,audio/wav" class="hidden" data-upload="${esc(voice.id)}">
          <button type="button" class="small" data-action="pickSample" data-id="${esc(voice.id)}">Sample hochladen</button>
        </label>
        <button class="small danger" data-action="deleteVoice" data-id="${esc(voice.id)}">Loeschen</button>
      </td>
    </tr>`).join("");

  return `
    <h1>Stimmen</h1>
    <p class="hint">Jede Stimme braucht ein WAV-Sample (~6-30s sauberes, deutsches Sprechmaterial) fuer das XTTS-Voice-Cloning. Nach dem Austausch eines Samples: Filler neu generieren.</p>
    <section class="block">
      <table>
        <thead><tr><th>ID</th><th>Name</th><th>Sprache</th><th>Status</th><th></th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </section>
    <section class="block">
      <h2>Neue Stimme</h2>
      <form class="grid" data-submit="createVoice">
        <label>ID (Dateiname, z. B. "papa") <input name="id" required pattern="[A-Za-z0-9_\\-]+"></label>
        <label>Name <input name="name" required></label>
        <label>Sprache <input name="language" value="de"></label>
        <div><button type="submit">Anlegen</button></div>
      </form>
    </section>`;
};

// ---- View: Filler & Trigger ------------------------------------------------------------------

views.fillers = async () => {
  const [triggers, fillers] = await Promise.all([
    api.get("/v1/admin/triggers"),
    api.get("/v1/admin/fillers"),
  ]);
  const kindLabel = { thinking: "Nachdenken", search: "Suche/RAG", tool: "Tool-Aufruf" };

  const triggerRows = triggers.map((trigger) => `
    <tr>
      <td>${esc(trigger.name)}</td>
      <td>${esc(kindLabel[trigger.kind] || trigger.kind)}</td>
      <td>${esc(trigger.tool_pattern || "-")}</td>
      <td class="actions">
        <button class="small danger" data-action="deleteTrigger" data-id="${trigger.id}" data-name="${esc(trigger.name)}">Loeschen</button>
      </td>
    </tr>`).join("");

  const triggerOptions = triggers.map((t) =>
    `<option value="${t.id}">${esc(t.name)}</option>`).join("");

  const fillerRows = fillers.map((filler) => {
    const audio = Object.entries(filler.audio_status || {});
    const generated = audio.filter(([, ok]) => ok).length;
    return `
    <tr>
      <td>${esc(filler.title)}<br><small>"${esc(filler.text)}"</small></td>
      <td>${esc(filler.trigger_name)}</td>
      <td>${filler.enabled ? '<span class="badge ok">aktiv</span>' : '<span class="badge off">aus</span>'}</td>
      <td>${generated}/${audio.length} Stimmen
        ${generated < audio.length ? '<span class="badge warn">unvollstaendig</span>' : '<span class="badge ok">bereit</span>'}</td>
      <td class="actions">
        <button class="small" data-action="generateFiller" data-id="${filler.id}">Audio generieren</button>
        <button class="small danger" data-action="deleteFiller" data-id="${filler.id}">Loeschen</button>
      </td>
    </tr>`;
  }).join("");

  return `
    <h1>Filler &amp; Trigger</h1>
    <p class="hint">Trigger bestimmen, WANN ein Filler gespielt wird - der Orchestrator kennt seinen Zustand selbst: "Nachdenken" (LLM langsam), "Suche/RAG", "Tool-Aufruf" (optional per Muster auf bestimmte Tools, z. B. <code>Calendar-*</code>; greift ab Tool-Calling 1.12). Filler werden per XTTS in jeder Stimme vorgeneriert - kein Stimmbruch mehr zwischen Filler und Antwort.</p>
    <section class="block">
      <h2>Filler</h2>
      <table>
        <thead><tr><th>Filler</th><th>Trigger</th><th>Status</th><th>Audio</th><th></th></tr></thead>
        <tbody>${fillerRows || "<tr><td colspan='5'>Noch keine Filler.</td></tr>"}</tbody>
      </table>
      <br>
      <form class="grid" data-submit="createFiller">
        <label>Titel <input name="title" required></label>
        <label>Trigger <select name="trigger_id">${triggerOptions}</select></label>
        <label class="full">Gesprochener Text <input name="text" required placeholder="Ich schaue kurz in den Kalender."></label>
        <div><button type="submit">Filler anlegen</button></div>
      </form>
    </section>
    <section class="block">
      <h2>Trigger</h2>
      <table>
        <thead><tr><th>Name</th><th>Zeitpunkt</th><th>Tool-Muster</th><th></th></tr></thead>
        <tbody>${triggerRows}</tbody>
      </table>
      <br>
      <form class="grid" data-submit="createTrigger">
        <label>Name <input name="name" required placeholder="Kalender"></label>
        <label>Zeitpunkt
          <select name="kind">
            <option value="thinking">Nachdenken (LLM langsam)</option>
            <option value="search">Suche/RAG</option>
            <option value="tool">Tool-Aufruf</option>
          </select>
        </label>
        <label>Tool-Muster (nur bei Tool-Aufruf) <input name="tool_pattern" placeholder="Calendar-*"></label>
        <div><button type="submit">Trigger anlegen</button></div>
      </form>
    </section>`;
};

// ---- View: Karten ------------------------------------------------------------------------

views.cards = async () => {
  const cards = await api.get("/v1/admin/cards");
  const rows = cards.map((card) => `
    <tr>
      <td>${esc(card.card_type)}</td>
      <td>v${card.layout_version}</td>
      <td class="actions">
        <button class="small ghost" data-action="editCard" data-type="${esc(card.card_type)}">Bearbeiten</button>
        <button class="small danger" data-action="deleteCard" data-type="${esc(card.card_type)}">Loeschen</button>
      </td>
    </tr>`).join("");

  return `
    <h1>Karten</h1>
    <p class="hint">Plattformneutrale Layout-Templates (4.12). Speichern erhoeht die globale Version - die Apps holen sich Aenderungen beim naechsten Start, ohne App-Update.</p>
    <section class="block">
      <table>
        <thead><tr><th>Kartentyp</th><th>Version</th><th></th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </section>
    <section class="block">
      <h2 id="card-form-title">Neue Karte / Layout einfuegen</h2>
      <form class="grid" data-submit="saveCard" id="card-form">
        <label>Kartentyp <input name="card_type" required pattern="[a-z0-9_]+" placeholder="shopping_list"></label>
        <label class="full">Layout-JSON (das "root"-Objekt, siehe docs/CARDS.md im App-Repo)
          <textarea name="root" rows="14" required placeholder='{"type": "column", "children": [...]}'></textarea>
        </label>
        <div><button type="submit">Speichern</button></div>
      </form>
    </section>
    <script type="application/json" id="cards-data">${JSON.stringify(cards)}</script>`;
};

// ---- Formular-/Button-Aktionen ----------------------------------------------------------------

const formActions = {
  saveCharacter: (form) => api.put("/v1/admin/character", { prompt: form.prompt.value }),

  async saveUser(form) {
    const id = form.id.value;
    const payload = {
      display_name: form.display_name.value,
      tier: parseInt(form.tier.value, 10),
      system_prompt_override: form.system_prompt_override.value || null,
      default_voice_id: form.default_voice_id.value || null,
    };
    if (id) {
      if (form.password.value) payload.password = form.password.value;
      await api.put(`/v1/admin/users/${id}`, payload);
    } else {
      await api.post("/v1/admin/users", {
        ...payload,
        username: form.username.value,
        password: form.password.value,
      });
    }
  },

  createVoice: (form) => api.post("/v1/admin/voices", {
    id: form.id.value, name: form.name.value, language: form.language.value || "de",
  }),

  createTrigger: (form) => api.post("/v1/admin/triggers", {
    name: form.name.value,
    kind: form.kind.value,
    tool_pattern: form.kind.value === "tool" ? (form.tool_pattern.value || "*") : null,
  }),

  createFiller: (form) => api.post("/v1/admin/fillers", {
    title: form.title.value,
    text: form.text.value,
    trigger_id: parseInt(form.trigger_id.value, 10),
    enabled: true,
  }),

  async saveCard(form) {
    let root;
    try {
      root = JSON.parse(form.root.value);
    } catch (_) {
      throw new Error("Layout-JSON ist kein gueltiges JSON.");
    }
    await api.post("/v1/admin/cards", { card_type: form.card_type.value, root });
  },
};

const buttonActions = {
  activateModel: (data) => api.post("/v1/admin/models/activate", { model: data.id }),

  editUser(data) {
    const users = JSON.parse(document.getElementById("users-data").textContent);
    const user = users.find((u) => u.id === parseInt(data.id, 10));
    const form = document.getElementById("user-form");
    form.id.value = user.id;
    form.username.value = user.username;
    form.username.disabled = true;
    form.display_name.value = user.display_name;
    form.tier.value = user.tier;
    form.default_voice_id.value = user.default_voice_id || "";
    form.system_prompt_override.value = user.system_prompt_override || "";
    document.getElementById("user-form-title").textContent = `Nutzer bearbeiten: ${user.username}`;
    form.scrollIntoView({ behavior: "smooth" });
  },

  resetUserForm() {
    const form = document.getElementById("user-form");
    form.reset();
    form.id.value = "";
    form.username.disabled = false;
    document.getElementById("user-form-title").textContent = "Neuen Nutzer anlegen";
  },

  async deleteUser(data) {
    if (!confirm(`Nutzer "${data.name}" wirklich loeschen?`)) return;
    await api.del(`/v1/admin/users/${data.id}`);
  },

  pickSample(data) {
    const input = document.querySelector(`input[data-upload="${data.id}"]`);
    input.onchange = async () => {
      if (!input.files.length) return;
      const body = new FormData();
      body.append("file", input.files[0]);
      try {
        await api.request("POST", `/v1/admin/voices/${data.id}/sample`, body, true);
        render();
      } catch (err) {
        alert(err.message);
      }
    };
    input.click();
  },

  async deleteVoice(data) {
    if (!confirm(`Stimme "${data.id}" loeschen? Das Sample wird mit entfernt.`)) return;
    await api.del(`/v1/admin/voices/${data.id}`);
  },

  async deleteTrigger(data) {
    if (!confirm(`Trigger "${data.name}" loeschen? Zugehoerige Filler werden mit geloescht.`)) return;
    await api.del(`/v1/admin/triggers/${data.id}`);
  },

  async generateFiller(data) {
    const result = await api.post(`/v1/admin/fillers/${data.id}/generate`, {});
    const failed = result.results.filter((r) => !r.ok);
    if (failed.length) {
      alert("Teilweise fehlgeschlagen:\n" + failed.map((f) => `${f.voice_id}: ${f.error}`).join("\n"));
    }
  },

  async deleteFiller(data) {
    if (!confirm("Filler loeschen?")) return;
    await api.del(`/v1/admin/fillers/${data.id}`);
  },

  editCard(data) {
    const cards = JSON.parse(document.getElementById("cards-data").textContent);
    const card = cards.find((c) => c.card_type === data.type);
    const form = document.getElementById("card-form");
    form.card_type.value = card.card_type;
    form.root.value = JSON.stringify(card.root, null, 2);
    document.getElementById("card-form-title").textContent = `Layout bearbeiten: ${card.card_type}`;
    form.scrollIntoView({ behavior: "smooth" });
  },

  async deleteCard(data) {
    if (!confirm(`Karte "${data.type}" loeschen? Apps fallen dann auf das generic-Template zurueck.`)) return;
    await api.del(`/v1/admin/cards/${encodeURIComponent(data.type)}`);
  },
};

// ---- Bootstrap -----------------------------------------------------------------------------

document.getElementById("login-form").addEventListener("submit", handleLogin);
document.getElementById("logout").addEventListener("click", logout);
window.addEventListener("hashchange", render);

if (api.token) {
  showApp();
} else {
  logout();
}
