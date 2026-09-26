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

  /* Binaerdaten (Filler-Audio, Probehoeren) MIT Token holen: ein
   * <audio src="..."> kann keinen Authorization-Header senden, deshalb
   * fetch + Blob-URL. Liefert die Response, damit auch Header lesbar sind. */
  async binary(method, path, body) {
    const headers = {};
    if (this.token) headers["Authorization"] = `Bearer ${this.token}`;
    if (body) headers["Content-Type"] = "application/json";
    const response = await fetch(path, {
      method,
      headers,
      body: body ? JSON.stringify(body) : undefined,
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
    return response;
  },

  async blob(path) {
    return (await this.binary("GET", path)).blob();
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
        if (!NO_RERENDER.has(form.dataset.submit)) render();
      } catch (err) {
        alert(err.message);
      }
    });
  });
  root.querySelectorAll("[data-action-change]").forEach((element) => {
    element.addEventListener("change", async () => {
      const action = element.dataset.actionChange;
      try {
        await buttonActions[action]?.(element.dataset, element);
        if (!NO_RERENDER.has(action)) render();
      } catch (err) {
        alert(err.message);
        render();
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

const NO_RERENDER = new Set(["editUser", "resetUserForm", "pickSample", "editCard",
                             "editFiller", "resetFillerForm", "editAgent",
                             "resetAgentForm", "switchCardFormat", "playFiller",
                             "filterFillers", "editVoice", "resetVoiceForm",
                             "previewTts", "editContractEngine"]);

/* Audio-Blob abspielen und die Blob-URL danach wieder freigeben, sonst
 * sammeln sich die Objekte im Tab an. */
async function playBlob(blob) {
  const url = URL.createObjectURL(blob);
  const audio = new Audio(url);
  audio.addEventListener("ended", () => URL.revokeObjectURL(url));
  audio.addEventListener("error", () => URL.revokeObjectURL(url));
  await audio.play();
}

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

// ---- View: Sprachausgabe ------------------------------------------------------------------

views.tts = async () => {
  const [data, voices] = await Promise.all([
    api.get("/v1/admin/tts"),
    api.get("/v1/admin/voices"),
  ]);
  const statusBadge = {
    ok: '<span class="badge ok">erreichbar</span>',
    loading: '<span class="badge warn">laedt Modell…</span>',
    error: '<span class="badge warn">Fehler</span>',
    unreachable: '<span class="badge off">nicht erreichbar</span>',
    off: '<span class="badge off">ausgeschaltet</span>',
    idle: '<span class="badge off">nicht geladen</span>',
  };

  const rows = data.engines.map((engine) => {
    const active = engine.id === data.active_engine;
    const detail = engine.status.detail ? `<br><small>${esc(engine.status.detail)}</small>` : "";
    return `<tr>
      <td><strong>${esc(engine.label)}</strong><br><small>${esc(engine.description)}</small></td>
      <td>${statusBadge[engine.status.status] || esc(engine.status.status)}${detail}</td>
      <td>${active ? '<span class="badge ok">aktiv</span>' : '<span class="badge off">inaktiv</span>'}</td>
      <td class="actions">
        ${active ? "" : `<button class="small" data-action="activateTts" data-id="${esc(engine.id)}">Aktivieren</button>`}
      </td>
    </tr>`;
  }).join("");

  // Engines nach dem Engine-Vertrag (v1.20): nur ID + Adresse, der Rest
  // kommt aus ihrem Steckbrief.
  const contractRows = data.contract_engines.map((engine) => `<tr>
      <td><code>${esc(engine.id)}</code></td>
      <td>${esc(engine.label || "")}</td>
      <td><code>${esc(engine.url)}</code></td>
      <td class="actions">
        <button class="small" data-action="editContractEngine" data-id="${esc(engine.id)}">Bearbeiten</button>
        <button class="small danger" data-action="deleteContractEngine" data-id="${esc(engine.id)}">Entfernen</button>
      </td>
    </tr>`).join("") || '<tr><td colspan="4" class="hint">Keine eingetragen.</td></tr>';

  const engineOptions = data.engines.map((e) =>
    `<option value="${esc(e.id)}" ${e.id === data.active_engine ? "selected" : ""}>${esc(e.label)}</option>`).join("");
  const voiceOptions = voices.map((v) => {
    const hint = v.sample_text ? "" : (v.has_sample ? " (ohne Transkript)" : " (ohne Sample)");
    return `<option value="${esc(v.id)}">${esc(v.name)}${esc(hint)}</option>`;
  }).join("");

  return `
    <h1>Sprachausgabe</h1>
    <p class="hint">Welche TTS-Engine die gesprochenen Antworten erzeugt - server-weit, wie das aktive LLM. <strong>Aktivieren</strong> laedt die Engine auf ihre Karte und entlaedt andere Sprachausgaben auf derselben Karte (beim ersten Mal kann das eine Minute dauern). Faellt die aktive Engine aus, bevor Audio geflossen ist (Container gestoppt, Modell laedt noch), spricht automatisch XTTS &ndash; bzw. Piper, solange XTTS entladen ist. Filler behalten ihre eigene Engine (Filler &amp; Trigger) - fuer eine einheitliche Stimme dort dieselbe Engine waehlen und neu generieren.</p>
    <section class="block">
      <table>
        <thead><tr><th>Engine</th><th>Dienst</th><th>Status</th><th></th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </section>
    <section class="block">
      <h2>Probehoeren &amp; vergleichen</h2>
      <p class="hint">Spricht einen Testsatz mit genau dieser Engine und Stimme (ohne XTTS-Fallback) und misst die Zeit bis zur ersten Sekunde Audio sowie fuer die komplette Synthese. Echtzeitfaktor unter 1 = schneller als Echtzeit. Eine nicht geladene Engine wird dafuer auf ihre Karte geladen, ohne andere zu entladen &ndash; ist die Karte voll, die Engine vorher aktivieren.</p>
      <form class="grid" data-submit="previewTts" id="tts-preview-form">
        <label>Engine <select name="engine">${engineOptions}</select></label>
        <label>Stimme <select name="voice_id">${voiceOptions}</select></label>
        <label class="full">Text
          <input name="text" required maxlength="500" value="Hallo! Ich bin deine Heim-Assistenz. Soll ich das Licht im Wohnzimmer einschalten?">
        </label>
        <div><button type="submit">Anhoeren</button></div>
        <p class="hint full" id="tts-preview-result"></p>
      </form>
    </section>
    <section class="block">
      <h2>Engines nach dem Engine-Vertrag</h2>
      <p class="hint">Jede Sprachausgabe, die den Engine-Vertrag spricht (<code>tts-engine-kit</code>, z. B. <code>tts-qwen3</code>), wird hier nur mit ID und Adresse eingetragen &ndash; Name, Sprachen und Faehigkeiten meldet sie selbst. Sie erscheint dann oben, beim Probehoeren, bei den Fillern und unter GPUs (Karte waehlen oder ganz aus).</p>
      <table>
        <thead><tr><th>ID</th><th>Name</th><th>Adresse</th><th></th></tr></thead>
        <tbody>${contractRows}</tbody>
      </table>
      <form class="grid" data-submit="saveContractEngine" id="contract-engine-form">
        <label>ID (klein, ohne Leerzeichen) <input name="id" required pattern="[a-z0-9][a-z0-9-]{0,30}" placeholder="qwen3" autocomplete="off" spellcheck="false"></label>
        <label>Name (optional) <input name="label" maxlength="60" placeholder="Qwen3-TTS"></label>
        <label class="full">Adresse &ndash; Container-Name:Port, IP:Port oder Domain <input name="url" required placeholder="http://tts-qwen3:8000" autocomplete="off" spellcheck="false"></label>
        <div><button type="submit">Speichern</button></div>
      </form>
    </section>
    <section class="block">
      <h2>Breeze TTS 2</h2>
      <p class="hint">Breeze klont eine Stimme nur mit Sample <strong>und</strong> exaktem Transkript (unter "Stimmen" pflegen); ohne Transkript spricht es mit seiner eingebauten Stimme. Offiziell unterstuetzt das Open-Weight-Modell nur Englisch und Chinesisch - deutsche Antworten koennen mit Akzent oder falsch ausgesprochen klingen. Der Server laeuft getrennt vom Voice-Stack, in einer von zwei Varianten mit derselben Schnittstelle: <strong>offizieller PyTorch-Server</strong> (<code>docker-compose.breeze.yml</code>, ~7,7 GB VRAM) oder <strong>Breeze-TTS-2.cpp</strong> (<code>docker-compose.breeze-cpp.yml</code> oder nativ, Q8_0 ~4 GB VRAM).</p>
      <form class="grid" data-submit="saveTtsSettings">
        <label class="full"><span>Breeze-Server - Container-Name, IP:Port oder Domain (leer = Standard aus der .env: ${esc(data.breeze_url_default)})</span>
          <input name="breeze_url" list="breeze-url-suggestions" value="${esc(data.breeze_url)}" placeholder="${esc(data.breeze_url_default)}" autocomplete="off" spellcheck="false">
          <datalist id="breeze-url-suggestions">
            <option value="http://tts-breeze:7860" label="Container: offizieller PyTorch-Server (docker-compose.breeze.yml)"></option>
            <option value="http://tts-breeze-cpp:7860" label="Container: Breeze-TTS-2.cpp (docker-compose.breeze-cpp.yml)"></option>
            <option value="http://192.168.2.105:7860" label="breeze-server nativ auf einem Rechner im LAN (IP anpassen)"></option>
          </datalist>
        </label>
        <p class="hint full">Genutzt wird gerade: <code>${esc(data.breeze_url_effective)}</code>. Nach dem Speichern zeigt die Tabelle oben, ob der Server unter dieser Adresse antwortet.</p>
        <label class="full">Sprechanweisung (optional, "Voice Direction") - steuert Tonfall, Tempo und Emotion; die Beispiele von Breeze sind auf Englisch. Breeze-TTS-2.cpp nutzt ohne Angabe "Speak clearly and naturally."
          <textarea name="breeze_instruction" rows="2" placeholder="Speak in a warm, calm and friendly tone.">${esc(data.breeze_instruction)}</textarea>
        </label>
        <div><button type="submit">Speichern</button></div>
      </form>
    </section>
    <script type="application/json" id="tts-data">${JSON.stringify({ engines: data.engines, fallback: data.fallback_engine, contract: data.contract_engines })}</script>`;
};

// ---- View: GPUs ------------------------------------------------------------------------

views.gpus = async () => {
  const data = await api.get("/v1/admin/gpus");

  const notice = data.nvml.available
    ? ""
    : `<div class="notice error">Keine VRAM-Anzeige: ${esc(data.nvml.reason)}</div>`;

  const gpuCards = data.gpus.map((gpu) => {
    const percent = gpu.memory_total_mb
      ? Math.round((gpu.memory_used_mb / gpu.memory_total_mb) * 100) : 0;
    const capability = gpu.compute_capability
      ? ` &middot; Compute ${gpu.compute_capability} &middot; ${esc(gpu.compute_type)}` : "";
    return `
      <div class="gpu-card">
        <h3>GPU ${gpu.index} &ndash; ${esc(gpu.name)}</h3>
        <div class="sub"><code>${esc(gpu.device)}</code>${capability}</div>
        <div class="vram-bar"><span class="${percent >= 85 ? "hot" : ""}" style="width:${percent}%"></span></div>
        <div class="vram-text">${gpu.memory_used_mb} / ${gpu.memory_total_mb} MB belegt &middot; ${gpu.memory_free_mb} MB frei</div>
      </div>`;
  }).join("");

  // Ohne NVML-Zugriff trotzdem zuweisbar bleiben: dann generische Eintraege.
  const choices = data.gpus.length
    ? data.gpus.map((gpu) => ({
        device: gpu.device,
        label: `GPU ${gpu.index} - ${gpu.name} (${gpu.memory_free_mb} MB frei)`,
      }))
    : [0, 1].map((index) => ({ device: `cuda:${index}`, label: `GPU ${index}` }));

  // "Aus" (v1.19) nur fuer Dienste, die ihr Modell entladen koennen (XTTS).
  const deviceOptions = (service) =>
    [
      ...(service.can_disable ? [{ device: "off", label: "Aus (Modell entladen)" }] : []),
      { device: "cpu", label: "CPU" },
      ...choices,
    ].map((choice) =>
      `<option value="${esc(choice.device)}" ${choice.device === service.assigned ? "selected" : ""}>${esc(choice.label)}</option>`
    ).join("");

  const rows = data.services.map((service) => {
    let control;
    if (!service.controllable) {
      const fixed = service.control_hint || (service.external ? "extern (Host)" : "CPU (fest)");
      control = `<span class="badge off">${esc(fixed)}</span>`;
    } else if (service.reachable) {
      control = `<select data-action-change="assignDevice" data-service="${esc(service.name)}" data-label="${esc(service.label)}">${deviceOptions(service)}</select>`;
    } else {
      control = '<span class="badge warn">nicht erreichbar</span>';
    }

    let running = "&ndash;";
    if (service.assigned === "off") {
      running = '<span class="badge off">aus &ndash; kein Modell geladen</span>';
    } else if (service.effective) {
      const deviates = service.assigned && service.effective !== service.assigned;
      running = `<code>${esc(service.effective)}</code>`;
      if (deviates) {
        running += ` <span class="badge warn">weicht ab</span>`;
      } else if (service.loaded) {
        running += ' <span class="badge ok">geladen</span>';
      } else {
        running += ' <span class="badge off">noch nicht geladen</span>';
      }
    } else if (service.state === "loading" || service.state === "preparing") {
      // Engines nach dem Engine-Vertrag melden, was sie gerade tun (v1.20).
      running = `<span class="badge warn">${service.state === "preparing" ? "laedt Gewichte herunter…" : "laedt…"}</span>`;
    } else if (service.state === "error") {
      running = `<span class="badge warn">${esc(service.detail || "Fehler beim Laden")}</span>`;
    } else if (service.state === "idle") {
      running = '<span class="badge off">nicht geladen</span>';
    } else if (service.error) {
      running = `<span class="badge warn">${esc(service.error)}</span>`;
    }

    return `
      <tr>
        <td>${esc(service.label)}<br><small>${esc(service.note)}</small></td>
        <td>${control}</td>
        <td>${running}</td>
      </tr>`;
  }).join("");

  return `
    <h1>GPUs &amp; Dienste</h1>
    <p class="hint">Verteilt die Dienste auf die vorhandenen Karten (VRAM-Budget 4.2). Der Wechsel laedt das Modell auf der neuen Karte neu &ndash; das dauert einige Sekunden, ein Container-Neustart ist nicht noetig, und die Zuweisung wird nach einem Neustart automatisch wiederhergestellt. Reicht das VRAM nicht, weicht der Dienst auf die CPU aus und die Spalte "Laeuft auf" zeigt "weicht ab". Sprachausgaben (XTTS und Engines nach dem Engine-Vertrag) lassen sich auch ganz ausschalten ("Aus"), solange sie nicht die Hauptstimme sprechen; "Aktivieren" unter Sprachausgabe laedt sie wieder.</p>
    ${notice}
    <section class="block">
      <h2>Grafikkarten</h2>
      <div class="gpu-grid">${gpuCards || "<p class='hint'>Keine Karten sichtbar.</p>"}</div>
    </section>
    <section class="block">
      <h2>Dienste</h2>
      <table>
        <thead><tr><th>Dienst</th><th>Zuweisung</th><th>Laeuft auf</th></tr></thead>
        <tbody>${rows}</tbody>
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
        <label class="full">System-Prompt (Antworten)
          <textarea name="prompt" rows="8">${esc(data.prompt)}</textarea>
        </label>
        <label class="full">System-Prompt fuer die Audio-Zusammenfassung
          <textarea name="voice_summary_prompt" rows="6" placeholder="Leer = eingebauter Standard">${esc(data.voice_summary_prompt)}</textarea>
        </label>
        <p class="hint full">Die Audio-Zusammenfassung wird gesprochen, wenn eine Antwort laenger als das Limit ist (Details bleiben im Chat). Wichtig: XTTS liest den Text WOERTLICH vor - der Prompt sollte Emojis, Formatierung und Emotions-Tags wie [froehlich] ausdruecklich verbieten (der Standard tut das).</p>
        <div><button type="submit">Speichern</button></div>
      </form>
    </section>`;
};

// ---- View: Nutzer ---------------------------------------------------------------------

views.users = async () => {
  const [users, voices, devices] = await Promise.all([
    api.get("/v1/admin/users"),
    api.get("/v1/admin/voices"),
    api.get("/v1/admin/devices"),
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
    <section class="block">
      <h2>Angemeldete Geraete</h2>
      <p class="hint">Jeder Login erzeugt ein langlebiges Geraete-Token (kein woechentliches Neu-Anmelden). "Abmelden" widerruft das Token sofort - z. B. bei einem verlorenen Handy. Ein Passwort-Reset meldet automatisch alle Geraete des Nutzers ab.</p>
      <table>
        <thead><tr><th>Nutzer</th><th>Geraet</th><th>Angemeldet</th><th>Zuletzt gesehen</th><th></th></tr></thead>
        <tbody>${devices.map((device) => `
          <tr>
            <td>${esc(device.username)}</td>
            <td>${esc(device.device_name || "(unbenannt)")}</td>
            <td>${esc(device.created_at)}</td>
            <td>${esc(device.last_seen_at)}</td>
            <td class="actions">
              <button class="small danger" data-action="revokeDevice" data-id="${device.id}" data-name="${esc(device.device_name || device.username)}">Abmelden</button>
            </td>
          </tr>`).join("") || "<tr><td colspan='5'>Keine Geraete angemeldet.</td></tr>"}</tbody>
      </table>
    </section>
    <script type="application/json" id="users-data">${JSON.stringify(users)}</script>`;
};

// ---- View: Stimmen ----------------------------------------------------------------------

views.voices = async () => {
  const voices = await api.get("/v1/admin/voices");
  const rows = voices.map((voice) => {
    let transcript;
    if (voice.sample_text) {
      const text = voice.sample_text.length > 90 ? `${voice.sample_text.slice(0, 90)}…` : voice.sample_text;
      transcript = `<small>"${esc(text)}"</small>`;
    } else {
      transcript = voice.has_sample
        ? '<span class="badge warn">fehlt</span>'
        : '<span class="badge off">-</span>';
    }
    return `
    <tr>
      <td>${esc(voice.id)}</td>
      <td>${esc(voice.name)}</td>
      <td>${esc(voice.language)}</td>
      <td>${voice.has_sample ? '<span class="badge ok">Sample vorhanden</span>' : '<span class="badge warn">kein Sample</span>'}</td>
      <td>${transcript}</td>
      <td class="actions">
        <label style="display:inline-block">
          <input type="file" accept=".wav,audio/wav" class="hidden" data-upload="${esc(voice.id)}">
          <button type="button" class="small" data-action="pickSample" data-id="${esc(voice.id)}">Sample hochladen</button>
        </label>
        ${voice.has_sample ? `<button class="small ghost" data-action="transcribeVoice" data-id="${esc(voice.id)}" title="Transkript per Whisper neu vorschlagen">Transkribieren</button>` : ""}
        <button class="small ghost" data-action="editVoice" data-id="${esc(voice.id)}">Bearbeiten</button>
        <button class="small danger" data-action="deleteVoice" data-id="${esc(voice.id)}">Loeschen</button>
      </td>
    </tr>`;
  }).join("");

  return `
    <h1>Stimmen</h1>
    <p class="hint">Jede Stimme braucht ein WAV-Sample (~6-30s sauberes, deutsches Sprechmaterial) fuer das Voice-Cloning. Breeze TTS 2 braucht zusaetzlich das <strong>exakte Transkript</strong> des Samples: Es wird beim Upload per Whisper vorgeschlagen und laesst sich unter "Bearbeiten" korrigieren (Wiederholungen und Versprecher mit aufschreiben). Nach dem Austausch eines Samples: Filler neu generieren.</p>
    <section class="block">
      <table>
        <thead><tr><th>ID</th><th>Name</th><th>Sprache</th><th>Status</th><th>Transkript (Breeze)</th><th></th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </section>
    <section class="block">
      <h2 id="voice-form-title">Neue Stimme</h2>
      <form class="grid" data-submit="saveVoice" id="voice-form">
        <input type="hidden" name="editing">
        <label>ID (Dateiname, z. B. "papa") <input name="id" required pattern="[A-Za-z0-9_\\-]+"></label>
        <label>Name <input name="name" required></label>
        <label>Sprache <input name="language" value="de"></label>
        <label class="full hidden" id="voice-transcript-label">Transkript des Samples - Wort fuer Wort, was im Sample gesprochen wird (leer = keins)
          <textarea name="sample_text" rows="3"></textarea>
        </label>
        <div><button type="submit">Speichern</button>
        <button type="button" class="ghost" data-action="resetVoiceForm">Neu</button></div>
      </form>
    </section>
    <script type="application/json" id="voices-data">${JSON.stringify(voices)}</script>`;
};

// ---- View: Filler & Trigger ------------------------------------------------------------------

/* Zustand der Filler-Ansicht (v1.16), ueberlebt das Neu-Rendern: gewaehlter
 * Trigger-Filter (auch im Browser gemerkt), laufende Generierung (sperrt
 * alle Generieren-Buttons) und die Ergebnis-Meldung der letzten. */
const fillerUi = {
  filter: readStored("heimai_filler_filter") || "all",
  job: null,     // {id, voice, label} der laufenden Generierung
  notice: null,  // {kind, lines} - wird beim naechsten Rendern einmal gezeigt
};

function readStored(key) {
  try { return localStorage.getItem(key); } catch (_) { return null; }
}

function writeStored(key, value) {
  try { localStorage.setItem(key, value); } catch (_) { /* privates Fenster o. ae. */ }
}

views.fillers = async () => {
  const [triggers, fillers, engines] = await Promise.all([
    api.get("/v1/admin/triggers"),
    api.get("/v1/admin/fillers"),
    api.get("/v1/admin/tts-engines"),
  ]);
  const kindLabel = { thinking: "Nachdenken", search: "Suche/RAG", tool: "Tool-Aufruf" };
  const engineLabel = Object.fromEntries(engines.map((e) => [e.id, e.label]));
  const engineOptions = engines.map((e) =>
    `<option value="${esc(e.id)}">${esc(e.label)}</option>`).join("");

  const triggerRows = triggers.map((trigger) => `
    <tr>
      <td>${esc(trigger.name)}</td>
      <td>${esc(kindLabel[trigger.kind] || trigger.kind)}</td>
      <td>${esc(trigger.tool_pattern || "-")}</td>
      <td class="actions">
        <button class="small danger" data-action="deleteTrigger" data-id="${trigger.id}" data-name="${esc(trigger.name)}">Loeschen</button>
      </td>
    </tr>`).join("");

  // Filter-Chips "Alle / Trigger 1 / Trigger 2 ..." (v1.16). Ein
  // geloeschter Trigger als gemerkte Auswahl faellt auf "Alle" zurueck.
  if (!triggers.some((t) => String(t.id) === fillerUi.filter)) fillerUi.filter = "all";
  const shown = (filler) => fillerUi.filter === "all" || String(filler.trigger_id) === fillerUi.filter;
  const countFor = (triggerId) => fillers.filter((f) => f.trigger_id === triggerId).length;
  const chip = (value, label, count, title = "") => {
    const active = value === fillerUi.filter;
    return `<button type="button" class="chip${active ? " active" : ""}" aria-pressed="${active}"
      data-action="filterFillers" data-trigger="${value}" title="${esc(title)}">${esc(label)} <span class="count">${count}</span></button>`;
  };
  const chips = [chip("all", "Alle", fillers.length)]
    .concat(triggers.map((t) => chip(String(t.id), t.name, countFor(t.id), kindLabel[t.kind] || t.kind)))
    .join("");

  // Neuer Filler landet standardmaessig im gerade gefilterten Trigger.
  const triggerOptions = triggers.map((t) =>
    `<option value="${t.id}"${String(t.id) === fillerUi.filter ? " selected" : ""}>${esc(t.name)}</option>`).join("");

  // Gruppiert nach Trigger (in Trigger-Reihenfolge): Auch unter "Alle"
  // stehen die Filler eines Triggers so beieinander.
  const triggerOrder = new Map(triggers.map((t, index) => [t.id, index]));
  fillers.sort((a, b) =>
    (triggerOrder.get(a.trigger_id) - triggerOrder.get(b.trigger_id)) || (a.id - b.id));

  const job = fillerUi.job;
  const generateButton = (fillerId, voiceId, label, title, extraClass = "") => {
    const running = job && job.id === String(fillerId) && job.voice === (voiceId || null);
    const voiceAttr = voiceId ? ` data-voice="${esc(voiceId)}"` : "";
    return `<button class="small ${extraClass}${running ? " busy" : ""}" data-action="generateFiller" data-id="${fillerId}"${voiceAttr} title="${esc(title)}" aria-label="${esc(title)}"${job ? " disabled" : ""}>${label}</button>`;
  };

  const fillerRows = fillers.map((filler) => {
    const audio = Object.entries(filler.audio_status || {});
    // Pro Stimme: Anhoeren (bzw. Warnung, wenn nichts generiert ist) und
    // NUR diese Stimme neu generieren - eine gelungene Stimme bleibt
    // erhalten, wenn eine andere neu gewuerfelt wird (v1.16).
    const audioCell = audio.map(([voiceId, ok]) => `
      <div class="voice-audio">
        ${ok
          ? `<button class="small ghost" data-action="playFiller" data-id="${filler.id}" data-voice="${esc(voiceId)}" title="Anhoeren">&#9654; ${esc(voiceId)}</button>`
          : `<span class="badge warn" title="noch nicht generiert">${esc(voiceId)}</span>`}
        ${generateButton(filler.id, voiceId, "&#8635;",
          ok ? `Nur ${voiceId} neu generieren` : `Nur ${voiceId} generieren`, "ghost icon")}
      </div>`
    ).join("") || '<span class="badge off">keine Stimmen angelegt</span>';

    return `
    <tr data-trigger="${filler.trigger_id}"${shown(filler) ? "" : ' class="hidden"'}>
      <td>${esc(filler.title)}<br><small>"${esc(filler.text)}"</small></td>
      <td>${esc(filler.trigger_name)}</td>
      <td class="nowrap" title="${esc(engineLabel[filler.engine] || "")}">${esc(engineShort(engineLabel[filler.engine] || filler.engine || "xtts"))}</td>
      <td class="nowrap">${filler.delay_ms} ms</td>
      <td>${filler.enabled ? '<span class="badge ok">aktiv</span>' : '<span class="badge off">aus</span>'}</td>
      <td>${audioCell}</td>
      <td class="actions">
        ${generateButton(filler.id, null, "Alle generieren", "Audio fuer alle Stimmen neu erzeugen")}
        <button class="small ghost" data-action="editFiller" data-id="${filler.id}">Bearbeiten</button>
        <button class="small danger" data-action="deleteFiller" data-id="${filler.id}">Loeschen</button>
      </td>
    </tr>`;
  }).join("");

  // Ergebnis der letzten Generierung einmalig anzeigen (statt alert):
  // Hinweise auf auffaellige Aufnahmen stehen so direkt ueber den
  // Play-Buttons, mit denen man sie pruefen kann.
  const notice = fillerUi.notice;
  fillerUi.notice = null;
  const statusHtml = job ? fillerJobNotice(job)
    : notice ? `<div class="notice ${notice.kind}">${notice.lines.map(esc).join("<br>")}</div>`
    : "";

  return `
    <h1>Filler &amp; Trigger</h1>
    <p class="hint">Trigger bestimmen, WANN ein Filler gespielt wird - der Orchestrator kennt seinen Zustand selbst: "Nachdenken" (LLM langsam), "Suche/RAG", "Tool-Aufruf" (optional per Muster auf bestimmte Tools, z. B. <code>Calendar-*</code>; greift ab Tool-Calling 1.12). Filler werden pro Stimme vorgeneriert - mit derselben Engine wie die Hauptstimme (Sprachausgabe) gibt es keinen hoerbaren Stimmbruch zwischen Filler und Antwort.</p>
    <section class="block">
      <h2>Filler</h2>
      <div class="chips" role="group" aria-label="Nach Trigger filtern">${chips}</div>
      <div id="filler-status">${statusHtml}</div>
      <table>
        <thead><tr><th>Filler</th><th>Trigger</th><th>Engine</th><th>Delay</th><th>Status</th><th>Audio je Stimme</th><th></th></tr></thead>
        <tbody id="filler-rows">${fillerRows}
          <tr id="filler-empty"${fillers.some(shown) ? ' class="hidden"' : ""}><td colspan="7">${fillers.length ? "Keine Filler fuer diesen Trigger." : "Noch keine Filler."}</td></tr>
        </tbody>
      </table>
      <br>
      <h2 id="filler-form-title">Neuen Filler anlegen</h2>
      <form class="grid" data-submit="saveFiller" id="filler-form">
        <input type="hidden" name="id">
        <label>Titel <input name="title" required></label>
        <label>Trigger <select name="trigger_id">${triggerOptions}</select></label>
        <label>Engine (womit das Audio erzeugt wird)
          <select name="engine">${engineOptions}</select>
        </label>
        <label>Delay (ms) - Wartezeit, bevor der Filler spielen darf; ist die Antwort/das Tool vorher fertig, entfaellt er. 0 = sofort
          <input name="delay_ms" type="number" min="0" max="60000" step="100" value="1200" required>
        </label>
        <label class="full">Gesprochener Text <input name="text" required placeholder="Ich schaue kurz in den Kalender."></label>
        <p class="hint full">${engines.map((e) => `<strong>${esc(e.label)}:</strong> ${esc(e.description)}`).join("<br>")}<br>
        Achtung: Beim Wechsel der Engine (oder des Textes) wird vorhandenes Audio verworfen - danach neu generieren.</p>
        <div><button type="submit">Speichern</button>
        <button type="button" class="ghost" data-action="resetFillerForm">Neu</button></div>
      </form>
      <script type="application/json" id="fillers-data">${JSON.stringify(fillers)}</script>
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

/* "XTTS-v2 (Stimme des Nutzers)" -> "XTTS-v2": in der Tabelle reicht der
 * Name, die Erklaerung steht im Tooltip und unter dem Formular. */
function engineShort(label) {
  return label.split(" (")[0];
}

function fillerById(id) {
  const data = document.getElementById("fillers-data");
  const fillers = data ? JSON.parse(data.textContent) : [];
  return fillers.find((f) => f.id === parseInt(id, 10)) || null;
}

function fillerJobNotice(job) {
  return `<div class="notice">${esc(job.label)} - je Stimme einige Sekunden; wirkt eine
    Aufnahme auffaellig (Zeitlupe, angehaengte Laute), wird bis zu zweimal neu gewuerfelt.</div>`;
}

/* Filter-Chip gewechselt: nur ein-/ausblenden, kein Neuladen der Seite. */
function applyFillerFilter() {
  const selected = fillerUi.filter;
  document.querySelectorAll("[data-action='filterFillers']").forEach((chip) => {
    const active = chip.dataset.trigger === selected;
    chip.classList.toggle("active", active);
    chip.setAttribute("aria-pressed", String(active));
  });
  let visible = 0;
  document.querySelectorAll("#filler-rows tr[data-trigger]").forEach((row) => {
    const shown = selected === "all" || row.dataset.trigger === selected;
    row.classList.toggle("hidden", !shown);
    if (shown) visible += 1;
  });
  document.getElementById("filler-empty").classList.toggle("hidden", visible > 0);
  const form = document.getElementById("filler-form");
  if (selected !== "all" && !form.id.value) form.trigger_id.value = selected;
}

/* Laufende Generierung im DOM spiegeln (ohne Neu-Rendern): alle
 * Generieren-Buttons gesperrt, der geklickte dreht sich. */
function markFillerJob() {
  const job = fillerUi.job;
  document.querySelectorAll("[data-action='generateFiller']").forEach((button) => {
    button.disabled = Boolean(job);
    button.classList.toggle("busy", Boolean(job) && button.dataset.id === job.id
      && (button.dataset.voice || null) === job.voice);
  });
  const status = document.getElementById("filler-status");
  if (status) status.innerHTML = job ? fillerJobNotice(job) : "";
}

function describeGeneration(filler, results) {
  let kind = "ok";
  const lines = results.map((r) => {
    if (!r.ok) {
      kind = "error";
      return `${r.voice_id}: fehlgeschlagen - ${r.error}`;
    }
    if (r.warning) {
      if (kind === "ok") kind = "warn";
      return `${r.voice_id}: ${r.warning}`;
    }
    return `${r.voice_id}: fertig${r.attempts > 1 ? ` (im ${r.attempts}. Versuch)` : ""}`;
  });
  const title = filler ? `"${filler.title}"` : "Filler";
  return { kind, lines: [`Audio fuer ${title}:`].concat(lines) };
}

// ---- View: Agenten -----------------------------------------------------------------------

views.agents = async () => {
  const agents = await api.get("/v1/admin/agents");
  let modelOptions = "";
  let modelNotice = "";
  try {
    const data = await api.get("/v1/admin/models");
    modelOptions = data.models.map((m) =>
      `<option value="${esc(m.id || "")}">${esc(m.id || "")}</option>`).join("");
  } catch (err) {
    modelNotice = `<div class="notice error">LiteLLM nicht erreichbar - Modell bitte von Hand eintragen: ${esc(err.message)}</div>`;
  }

  const rows = agents.map((agent) => `
    <tr>
      <td><code>agent-${esc(agent.slug)}</code><br><small>${esc(agent.name)}</small></td>
      <td>${esc(agent.description)}</td>
      <td>${esc(agent.model)}</td>
      <td>${agent.enabled ? '<span class="badge ok">aktiv</span>' : '<span class="badge off">aus</span>'}</td>
      <td class="actions">
        <button class="small ghost" data-action="editAgent" data-id="${esc(agent.slug)}">Bearbeiten</button>
        <button class="small danger" data-action="deleteAgent" data-id="${esc(agent.slug)}">Loeschen</button>
      </td>
    </tr>`).join("");

  return `
    <h1>Agenten</h1>
    <p class="hint">Spezialisierte Helfer mit eigenem Modell und Prompt (4.16). Jeder aktive Agent erscheint der Haupt-KI als Tool <code>agent-&lt;id&gt;</code> - sie waehlt ihn anhand der Beschreibung aus. Damit lassen sich unpersoenliche Aufgaben (Websuche, Coding) gezielt an Cloud-Modelle delegieren und die Heim-KI entlasten. Agenten duerfen die Server-Tools (MCP) nutzen, aber keine Geraete-Tools oder Karten. <strong>Reservierte ID <code>code-card</code>:</strong> Existiert ein aktiver Agent mit dieser ID, schreibt ER automatisch die HTML-Layouts fuer Karten ohne passendes Template - die Haupt-KI liefert nur Titel + Daten.</p>
    ${modelNotice}
    <section class="block">
      <table>
        <thead><tr><th>Agent</th><th>Beschreibung</th><th>Modell</th><th>Status</th><th></th></tr></thead>
        <tbody>${rows || "<tr><td colspan='5'>Noch keine Agenten.</td></tr>"}</tbody>
      </table>
    </section>
    <section class="block">
      <h2 id="agent-form-title">Neuen Agenten anlegen</h2>
      <form class="grid" data-submit="saveAgent" id="agent-form">
        <input type="hidden" name="editing">
        <label>ID (fuer die KI, z. B. "websuche" oder "coding") <input name="slug" required pattern="[a-z0-9_\\-]{1,40}"></label>
        <label>Name <input name="name" required placeholder="Websuche-Agent"></label>
        <label>Modell ${modelOptions
          ? `<select name="model">${modelOptions}</select>`
          : `<input name="model" required placeholder="mistral/mistral-large-latest">`}</label>
        <label>Aktiv
          <select name="enabled"><option value="1">Ja</option><option value="0">Nein</option></select>
        </label>
        <label class="full">Beschreibung (danach waehlt die Haupt-KI den Agenten aus)
          <textarea name="description" rows="2" required placeholder="Recherchiert aktuelle Informationen im Web und liefert eine Zusammenfassung mit Quellen."></textarea>
        </label>
        <label class="full">System-Prompt des Agenten
          <textarea name="system_prompt" rows="6" placeholder="Du bist ein Recherche-Agent. Antworte sachlich, nenne Quellen..."></textarea>
        </label>
        <div><button type="submit">Speichern</button>
        <button type="button" class="ghost" data-action="resetAgentForm">Neu</button></div>
      </form>
    </section>
    <script type="application/json" id="agents-data">${JSON.stringify(agents)}</script>`;
};

// ---- View: Karten ------------------------------------------------------------------------

views.cards = async () => {
  const cards = await api.get("/v1/admin/cards");
  const rows = cards.map((card) => `
    <tr>
      <td>${esc(card.card_type)}</td>
      <td>${card.format === "html" ? '<span class="badge ok">HTML</span>' : "JSON"}</td>
      <td>v${card.layout_version}</td>
      <td class="actions">
        <button class="small ghost" data-action="editCard" data-type="${esc(card.card_type)}">Bearbeiten</button>
        <button class="small danger" data-action="deleteCard" data-type="${esc(card.card_type)}">Loeschen</button>
      </td>
    </tr>`).join("");

  return `
    <h1>Karten</h1>
    <p class="hint">Plattformneutrale Layout-Templates (4.12). Speichern erhoeht die globale Version - die Apps holen sich Aenderungen beim naechsten Start, ohne App-Update. Neben Layout-JSON gehen jetzt auch <strong>HTML-Karten</strong>: ein HTML-Fragment mit {{data.*}}-Platzhaltern, gerendert in einer Sandbox (Web-UI/Windows; die Android-App zeigt bis zu ihrem Update die generic-Karte). Findet die KI keinen passenden Kartentyp, uebernimmt der Agent <code>code-card</code> (unter "Agenten" anlegen) das Schreiben des HTML-Layouts automatisch.</p>
    <section class="block">
      <table>
        <thead><tr><th>Kartentyp</th><th>Format</th><th>Version</th><th></th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </section>
    <section class="block">
      <h2 id="card-form-title">Neue Karte / Layout einfuegen</h2>
      <form class="grid" data-submit="saveCard" id="card-form">
        <label>Kartentyp <input name="card_type" required pattern="[a-z0-9_]+" placeholder="shopping_list"></label>
        <label>Format
          <select name="format" data-action-change="switchCardFormat">
            <option value="json">JSON (Layout-Baum)</option>
            <option value="html">HTML (Fragment mit {{data.*}})</option>
          </select>
        </label>
        <label class="full" id="card-root-label">Layout-JSON (das "root"-Objekt, siehe docs/CARDS.md im App-Repo)
          <textarea name="root" rows="14" placeholder='{"type": "column", "children": [...]}'></textarea>
        </label>
        <label class="full hidden" id="card-html-label">HTML-Fragment (Platzhalter wie {{data.headline}} werden clientseitig ersetzt; Inline-CSS erlaubt)
          <textarea name="html" rows="14" placeholder='&lt;div style="font-family:sans-serif"&gt;&lt;h3&gt;{{data.headline}}&lt;/h3&gt;&lt;p&gt;{{data.body}}&lt;/p&gt;&lt;/div&gt;'></textarea>
        </label>
        <div><button type="submit">Speichern</button></div>
      </form>
    </section>
    <script type="application/json" id="cards-data">${JSON.stringify(cards)}</script>`;
};

// ---- Formular-/Button-Aktionen ----------------------------------------------------------------

const formActions = {
  saveCharacter: (form) => api.put("/v1/admin/character", {
    prompt: form.prompt.value,
    voice_summary_prompt: form.voice_summary_prompt.value,
  }),

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

  async saveVoice(form) {
    if (form.editing.value) {
      await api.put(`/v1/admin/voices/${encodeURIComponent(form.id.value)}`, {
        name: form.name.value,
        language: form.language.value || "de",
        sample_text: form.sample_text.value,
      });
    } else {
      await api.post("/v1/admin/voices", {
        id: form.id.value, name: form.name.value, language: form.language.value || "de",
      });
    }
  },

  async previewTts(form) {
    const result = document.getElementById("tts-preview-result");
    const button = form.querySelector("button[type=submit]");
    button.disabled = true;
    result.textContent = "Erzeuge Audio…";
    try {
      const response = await api.binary("POST", "/v1/admin/tts/preview", {
        engine: form.engine.value,
        voice_id: form.voice_id.value,
        text: form.text.value,
      });
      const first = Number(response.headers.get("X-TTS-First-Chunk-Ms"));
      const total = Number(response.headers.get("X-TTS-Total-Ms"));
      const audio = Number(response.headers.get("X-Audio-Ms"));
      const rtf = audio ? (total / audio).toFixed(2) : "-";
      result.textContent = `${form.engine.selectedOptions[0].textContent}: erste Sekunde Audio nach ${first} ms, `
        + `komplette Synthese ${total} ms fuer ${(audio / 1000).toFixed(1)} s Audio (Echtzeitfaktor ${rtf}).`;
      await playBlob(await response.blob());
    } catch (err) {
      result.textContent = "";
      throw err;
    } finally {
      button.disabled = false;
    }
  },

  saveContractEngine: (form) => api.post("/v1/admin/tts/engines", {
    id: form.id.value.trim(),
    url: form.url.value,
    label: form.label.value.trim() || null,
  }),

  saveTtsSettings: (form) => api.put("/v1/admin/tts/settings", {
    breeze_url: form.breeze_url.value,
    breeze_instruction: form.breeze_instruction.value,
  }),

  createTrigger: (form) => api.post("/v1/admin/triggers", {
    name: form.name.value,
    kind: form.kind.value,
    tool_pattern: form.kind.value === "tool" ? (form.tool_pattern.value || "*") : null,
  }),

  async saveFiller(form) {
    const payload = {
      title: form.title.value,
      text: form.text.value,
      trigger_id: parseInt(form.trigger_id.value, 10),
      delay_ms: parseInt(form.delay_ms.value, 10),
      engine: form.engine.value,
      enabled: true,
    };
    if (form.id.value) {
      await api.put(`/v1/admin/fillers/${form.id.value}`, payload);
    } else {
      await api.post("/v1/admin/fillers", payload);
    }
  },

  async saveCard(form) {
    const format = form.format.value;
    const payload = { card_type: form.card_type.value, format };
    if (format === "html") {
      if (!form.html.value.trim()) throw new Error("HTML-Inhalt fehlt.");
      payload.html = form.html.value;
      payload.root = {};
    } else {
      try {
        payload.root = JSON.parse(form.root.value);
      } catch (_) {
        throw new Error("Layout-JSON ist kein gueltiges JSON.");
      }
    }
    await api.post("/v1/admin/cards", payload);
  },

  async saveAgent(form) {
    const payload = {
      slug: form.slug.value,
      name: form.name.value,
      description: form.description.value,
      system_prompt: form.system_prompt.value,
      model: form.model.value,
      enabled: form.enabled.value === "1",
    };
    if (form.editing.value) {
      await api.put(`/v1/admin/agents/${encodeURIComponent(payload.slug)}`, payload);
    } else {
      await api.post("/v1/admin/agents", payload);
    }
  },
};

const buttonActions = {
  activateModel: (data) => api.post("/v1/admin/models/activate", { model: data.id }),

  /* Eine gerade nicht erreichbare Engine erst nach Rueckfrage aktivieren -
   * vorab waehlen bleibt moeglich, aber nicht aus Versehen. */
  async activateTts(data) {
    const { engines, fallback } = JSON.parse(document.getElementById("tts-data").textContent);
    const engine = engines.find((e) => e.id === data.id);
    const fallbackEngine = engines.find((e) => e.id === fallback);
    let confirmed = false;
    // "nicht geladen"/"aus" ist kein Problem - Aktivieren laedt die Engine.
    if (engine && ["unreachable", "error"].includes(engine.status.status)) {
      // Rueckfallebene ist XTTS - oder Piper, solange XTTS ausgeschaltet ist.
      const meanwhile = engine.id === fallback
        ? "Bis dahin liest die App die Antworten selbst vor."
        : `Bis der Dienst laeuft, spricht ${fallbackEngine ? fallbackEngine.label : fallback}.`;
      if (!confirm(`${engine.label} ist gerade nicht erreichbar:\n\n${engine.status.detail}`
        + `\n\nTrotzdem aktivieren? ${meanwhile}`)) return;
      confirmed = true;
    }
    const button = document.querySelector(`button[data-action="activateTts"][data-id="${CSS.escape(data.id)}"]`);
    if (button) {
      button.disabled = true;
      button.textContent = "laedt…";
    }
    const result = await api.post("/v1/admin/tts/activate", { engine: data.id });
    const messages = [...(result.notes || [])];
    if (result.warning && !confirmed) messages.push(result.warning);
    if (messages.length) alert(messages.join("\n\n"));
  },

  editContractEngine(data) {
    const { contract } = JSON.parse(document.getElementById("tts-data").textContent);
    const engine = contract.find((e) => e.id === data.id);
    const form = document.getElementById("contract-engine-form");
    form.id.value = engine.id;
    form.label.value = engine.label || "";
    form.url.value = engine.url;
    form.scrollIntoView({ behavior: "smooth" });
  },

  async deleteContractEngine(data) {
    if (!confirm(`Engine "${data.id}" entfernen? Der Container laeuft weiter, der Orchestrator nutzt ihn nur nicht mehr.`)) return;
    await api.del(`/v1/admin/tts/engines/${encodeURIComponent(data.id)}`);
  },

  async assignDevice(data, element) {
    // Abbrechen rendert neu - die Auswahl springt dann zurueck.
    if (element.value === "off" && !confirm(
      `${data.label} ausschalten?\n\nDas Modell wird entladen und gibt sein VRAM frei. `
      + "Faellt die aktive Sprachausgabe aus, springt dann Piper ein; vorhandene Filler "
      + "bleiben abspielbar. Wieder einschalten: hier eine Karte waehlen.")) return;
    // Das Neuladen des Modells dauert - Zeile sichtbar "beschaeftigt"
    // stellen, sonst wirkt das Panel eingefroren.
    element.disabled = true;
    const row = element.closest("tr");
    if (row) row.style.opacity = "0.5";
    await api.put(`/v1/admin/gpus/${encodeURIComponent(data.service)}`, {
      device: element.value,
    });
  },

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

  async revokeDevice(data) {
    if (!confirm(`Geraet "${data.name}" abmelden? Es muss sich danach neu einloggen.`)) return;
    await api.del(`/v1/admin/devices/${data.id}`);
  },

  pickSample(data) {
    const input = document.querySelector(`input[data-upload="${data.id}"]`);
    input.onchange = async () => {
      if (!input.files.length) return;
      const body = new FormData();
      body.append("file", input.files[0]);
      try {
        const result = await api.request("POST", `/v1/admin/voices/${data.id}/sample`, body, true);
        if (result.transcript_error) {
          alert("Sample gespeichert, aber kein Transkript-Vorschlag moeglich: "
            + `${result.transcript_error}\nFuer Breeze bitte unter "Bearbeiten" von Hand eintragen.`);
        }
        render();
      } catch (err) {
        alert(err.message);
      }
    };
    input.click();
  },

  transcribeVoice: (data) =>
    api.post(`/v1/admin/voices/${encodeURIComponent(data.id)}/transcribe`, {}),

  editVoice(data) {
    const voices = JSON.parse(document.getElementById("voices-data").textContent);
    const voice = voices.find((v) => v.id === data.id);
    const form = document.getElementById("voice-form");
    form.editing.value = "1";
    form.id.value = voice.id;
    form.id.readOnly = true;
    form.name.value = voice.name;
    form.language.value = voice.language;
    form.sample_text.value = voice.sample_text || "";
    document.getElementById("voice-transcript-label").classList.remove("hidden");
    document.getElementById("voice-form-title").textContent = `Stimme bearbeiten: ${voice.name}`;
    form.scrollIntoView({ behavior: "smooth" });
  },

  resetVoiceForm() {
    const form = document.getElementById("voice-form");
    form.reset();
    form.editing.value = "";
    form.id.readOnly = false;
    document.getElementById("voice-transcript-label").classList.add("hidden");
    document.getElementById("voice-form-title").textContent = "Neue Stimme";
  },

  async deleteVoice(data) {
    if (!confirm(`Stimme "${data.id}" loeschen? Das Sample wird mit entfernt.`)) return;
    await api.del(`/v1/admin/voices/${data.id}`);
  },

  async deleteTrigger(data) {
    if (!confirm(`Trigger "${data.name}" loeschen? Zugehoerige Filler werden mit geloescht.`)) return;
    await api.del(`/v1/admin/triggers/${data.id}`);
  },

  editFiller(data) {
    const fillers = JSON.parse(document.getElementById("fillers-data").textContent);
    const filler = fillers.find((f) => f.id === parseInt(data.id, 10));
    const form = document.getElementById("filler-form");
    form.id.value = filler.id;
    form.title.value = filler.title;
    form.text.value = filler.text;
    form.trigger_id.value = filler.trigger_id;
    form.delay_ms.value = filler.delay_ms;
    form.engine.value = filler.engine || "xtts";
    document.getElementById("filler-form-title").textContent = `Filler bearbeiten: ${filler.title}`;
    form.scrollIntoView({ behavior: "smooth" });
  },

  resetFillerForm() {
    const form = document.getElementById("filler-form");
    form.reset();
    form.id.value = "";
    form.delay_ms.value = 1200;
    if (fillerUi.filter !== "all") form.trigger_id.value = fillerUi.filter;
    document.getElementById("filler-form-title").textContent = "Neuen Filler anlegen";
  },

  filterFillers(data) {
    fillerUi.filter = data.trigger;
    writeStored("heimai_filler_filter", data.trigger);
    applyFillerFilter();
  },

  /* Alle Stimmen oder mit data-voice nur eine (v1.16) - eine gelungene
   * Stimme bleibt erhalten, wenn eine andere neu gewuerfelt wird. Immer nur
   * eine Generierung zur Zeit: Der Server serialisiert ohnehin, so sieht
   * man aber, dass noch etwas laeuft, statt mehrfach zu klicken. */
  async generateFiller(data) {
    if (fillerUi.job) return;
    const filler = fillerById(data.id);
    const target = data.voice ? `Stimme ${data.voice}` : "alle Stimmen";
    fillerUi.job = {
      id: data.id,
      voice: data.voice || null,
      label: `Generiere ${filler ? `"${filler.title}"` : "Filler"} (${target})`,
    };
    markFillerJob();
    try {
      const query = data.voice ? `?voice_id=${encodeURIComponent(data.voice)}` : "";
      const result = await api.post(`/v1/admin/fillers/${data.id}/generate${query}`, {});
      fillerUi.notice = describeGeneration(filler, result.results);
    } finally {
      fillerUi.job = null;
      markFillerJob();
    }
  },

  /* Generiertes Audio anhoeren - deckt misslungene Generierungen auf,
   * bevor sie im Realtime-Talk auffallen. */
  async playFiller(data) {
    await playBlob(await api.blob(
      `/v1/admin/fillers/${data.id}/audio?voice_id=${encodeURIComponent(data.voice)}`
    ));
  },

  async deleteFiller(data) {
    if (!confirm("Filler loeschen?")) return;
    await api.del(`/v1/admin/fillers/${data.id}`);
  },

  switchCardFormat() {
    const form = document.getElementById("card-form");
    const isHtml = form.format.value === "html";
    document.getElementById("card-root-label").classList.toggle("hidden", isHtml);
    document.getElementById("card-html-label").classList.toggle("hidden", !isHtml);
  },

  editCard(data) {
    const cards = JSON.parse(document.getElementById("cards-data").textContent);
    const card = cards.find((c) => c.card_type === data.type);
    const form = document.getElementById("card-form");
    form.card_type.value = card.card_type;
    form.format.value = card.format || "json";
    form.root.value = JSON.stringify(card.root, null, 2);
    form.html.value = card.html || "";
    buttonActions.switchCardFormat();
    document.getElementById("card-form-title").textContent = `Layout bearbeiten: ${card.card_type}`;
    form.scrollIntoView({ behavior: "smooth" });
  },

  editAgent(data) {
    const agents = JSON.parse(document.getElementById("agents-data").textContent);
    const agent = agents.find((a) => a.slug === data.id);
    const form = document.getElementById("agent-form");
    form.editing.value = "1";
    form.slug.value = agent.slug;
    form.slug.readOnly = true;
    form.name.value = agent.name;
    form.description.value = agent.description;
    form.system_prompt.value = agent.system_prompt;
    form.model.value = agent.model;
    form.enabled.value = agent.enabled ? "1" : "0";
    document.getElementById("agent-form-title").textContent = `Agent bearbeiten: ${agent.name}`;
    form.scrollIntoView({ behavior: "smooth" });
  },

  resetAgentForm() {
    const form = document.getElementById("agent-form");
    form.reset();
    form.editing.value = "";
    form.slug.readOnly = false;
    document.getElementById("agent-form-title").textContent = "Neuen Agenten anlegen";
  },

  async deleteAgent(data) {
    if (!confirm(`Agent "${data.id}" loeschen?`)) return;
    await api.del(`/v1/admin/agents/${encodeURIComponent(data.id)}`);
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
