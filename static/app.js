"use strict";
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const eur = new Intl.NumberFormat("fr-FR", { style: "currency", currency: "EUR" });
const money = (v) => (v == null ? "—" : eur.format(v));
/** Date du prix chez Cardmarket : si elle est ancienne, on le dit (la source ne met pas tout à jour chaque jour). */
function priceNote(d, game) {
  // One Piece : pas de prix Cardmarket gratuit, on affiche le prix TCGplayer (USD) converti en euros
  const src = game === "onepiece" ? "Prix TCGplayer converti en €, estimation" : "Prix Cardmarket";
  const t = d ? new Date(d) : null;
  if (!t || isNaN(t)) return game === "onepiece" ? `<div class="small muted">${src}</div>` : "";
  const days = Math.floor((Date.now() - t) / 864e5);
  const old = days > 14;
  return `<div class="small ${old ? "" : "muted"}" ${old ? 'style="color:var(--accent)"' : ""}>${src} du ${t.toLocaleDateString("fr-FR")}` +
    `${old ? ` — ancien (${days} j) : la source ne l'a pas actualisé` : ""}</div>`;
}
/** Saisie d'un prix à la main : « 12,50 », « 12.5 », « 12,50 € ». Renvoie un nombre >= 0, ou null si vide / invalide. */
function parseMoney(t) {
  const x = String(t ?? "").replace(/[€\s\u00a0]/g, "").replace(",", ".");
  if (x === "" || !/^\d+(\.\d+)?$/.test(x)) return null;
  const n = Math.round(parseFloat(x) * 100) / 100;
  return isFinite(n) && n <= 1e7 ? n : null;
}
/** Bloc « Utiliser mon propre prix » (interrupteur + champ), commun à l'ajout et à la fiche. */
function customPriceHtml(market, on, value) {
  return `<div class="custom">
    <label class="switch"><input type="checkbox" id="p-use" ${on ? "checked" : ""}><span>Utiliser mon propre prix</span></label>
    <div id="p-wrap" class="${on ? "" : "hidden"}"><input id="p-val" type="text" inputmode="decimal" autocomplete="off" placeholder="ex. 12,50" value="${value != null ? esc(String(value).replace(".", ",")) : ""}"> <span class="muted">€ l'unité</span></div>
    <div class="muted small">Prix du marché : ${money(market)}${on ? " (non utilisé)" : ""}</div>
  </div>`;
}
function wireCustomPrice(dlg) {
  $("#p-use", dlg).onchange = () => {
    const on = $("#p-use", dlg).checked;
    $("#p-wrap", dlg).classList.toggle("hidden", !on);
    if (on) $("#p-val", dlg).focus();
  };
}
/** Lit le bloc : {use_custom, custom_price} à envoyer au serveur. Lève une erreur si l'interrupteur est activé sans prix valide. */
function readCustomPrice(dlg) {
  const on = $("#p-use", dlg).checked;
  const v = parseMoney($("#p-val", dlg).value);
  if (on && v == null) { $("#p-val", dlg).focus(); throw new Error("Indique ton prix (ex. 12,50) ou désactive l'option"); }
  return { use_custom: on, ...(v != null ? { custom_price: v } : {}) };
}
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const CONDITIONS = [["MT", "Mint"], ["NM", "Near Mint"], ["EX", "Excellent"], ["GD", "Good"], ["LP", "Light Played"], ["PL", "Played"], ["PO", "Poor"]];
/** Nom à afficher : français si on le connaît (Pokémon), sinon celui de la base. */
const dn = (x) => x.name_fr || x.name;
const GAME_LABEL = { pokemon: "Pokémon", yugioh: "Yu-Gi-Oh!", magic: "Magic", onepiece: "One Piece" };

let items = [];
let me = null; // membre connecté
let sessionInfo = {};
let addGame = "pokemon";
let filterGame = "";
let sumChart = null;
let dlgChart = null;

/* ------------------------------------------------------------- réseau */
async function api(path, opts = {}) {
  const res = await fetch("/api" + path, {
    credentials: "same-origin",
    headers: opts.body ? { "Content-Type": "application/json" } : {},
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (res.status === 401) { showLogin(); throw new Error("non connecté"); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || "Erreur " + res.status);
  return data;
}

function toast(msg, ms = 2600) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.add("hidden"), ms);
}

/* ------------------------------------------------------------- connexion */
let loginMode = "login";
function setLoginMode(m) {
  loginMode = m;
  $$("#login-mode button").forEach((x) => x.classList.toggle("on", x.dataset.m === m));
  const signup = m === "signup";
  $("#login-code").classList.toggle("hidden", !(signup && sessionInfo.signup_code));
  $("#login-code").required = signup && !!sessionInfo.signup_code;
  $("#login-pwd").autocomplete = signup ? "new-password" : "current-password";
  $("#login-btn").textContent = signup ? "Créer mon compte" : "Entrer";
  const hint = signup
    ? (sessionInfo.has_users ? "" : "Premier compte : il récupère la collection déjà enregistrée.") +
      (sessionInfo.signup_code ? " Le code d'invitation t'est donné par la personne qui héberge l'appli." : "")
    : "";
  $("#login-hint").textContent = hint.trim();
  $("#login-hint").classList.toggle("hidden", !hint.trim());
  $("#login-err").textContent = "";
}
$$("#login-mode button").forEach((b) => b.addEventListener("click", () => setLoginMode(b.dataset.m)));

function showLogin() {
  $("#app").classList.add("hidden"); $("#login").classList.remove("hidden");
  setLoginMode(sessionInfo.has_users === false ? "signup" : loginMode);
}
function showApp() {
  $("#login").classList.add("hidden"); $("#app").classList.remove("hidden");
  $("#btn-me").textContent = me ? me.username.slice(0, 1) : "?";
}

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("#login-err").textContent = "";
  try {
    const body = { username: $("#login-user").value.trim(), password: $("#login-pwd").value, code: $("#login-code").value.trim() };
    const r = await api(loginMode === "signup" ? "/register" : "/login", { method: "POST", body });
    me = r.user;
    $("#login-pwd").value = "";
    showApp();
    loadAll();
  } catch (err) { $("#login-err").textContent = err.message; }
});

/* ------------------------------------------------------------- mon compte */
$("#btn-me").addEventListener("click", () => {
  const d = $("#dlg-me");
  d.innerHTML = `
    <h2 style="margin:0 0 12px">${esc(me.username)}</h2>
    <div class="custom">
      <label class="switch"><input type="checkbox" id="me-public" ${me.is_public ? "checked" : ""}><span>Les autres membres peuvent voir ma collection</span></label>
      <div class="muted small" style="margin-top:6px">Ils voient tes cartes et leur valeur, jamais tes notes.</div>
    </div>
    <details style="margin:10px 0">
      <summary class="muted">Changer de mot de passe</summary>
      <label class="field">Mot de passe actuel<input id="me-old" type="password" autocomplete="current-password"></label>
      <label class="field">Nouveau mot de passe (8 caractères min.)<input id="me-new" type="password" autocomplete="new-password"></label>
      <button class="btn" id="me-pwd" type="button">Changer</button>
    </details>
    <div class="dlg-actions">
      <button class="btn danger" id="me-out">Se déconnecter</button>
      <button class="btn" id="me-close">Fermer</button>
    </div>`;
  d.showModal();
  $("#me-close", d).onclick = () => d.close();
  $("#me-public", d).onchange = async (e) => {
    try { me = (await api("/me", { method: "PATCH", body: { is_public: e.target.checked } })).user; toast(me.is_public ? "Collection visible par les membres" : "Collection privée"); }
    catch (err) { e.target.checked = !e.target.checked; toast(err.message); }
  };
  $("#me-pwd", d).onclick = async () => {
    try {
      await api("/me", { method: "PATCH", body: { password: $("#me-old", d).value, new_password: $("#me-new", d).value } });
      toast("Mot de passe changé ✓"); d.close();
    } catch (err) { toast(err.message); }
  };
  $("#me-out", d).onclick = async () => {
    await api("/logout", { method: "POST" }).catch(() => {});
    me = null; items = []; d.close(); showLogin();
  };
});

/* ------------------------------------------------------------- onglets */
$$("nav button").forEach((b) => b.addEventListener("click", () => {
  $$("nav button").forEach((x) => x.classList.toggle("on", x === b));
  $("#tab-collection").classList.toggle("hidden", b.dataset.tab !== "collection");
  $("#tab-add").classList.toggle("hidden", b.dataset.tab !== "add");
  $("#tab-members").classList.toggle("hidden", b.dataset.tab !== "members");
  if (b.dataset.tab === "members") loadMembers();
  window.scrollTo(0, 0);
}));

/* ------------------------------------------------------------- collection */
const varBadge = (label, v) => {
  if (v == null) return `<span class="badge">${label} —</span>`;
  const cls = v > 0 ? "up" : v < 0 ? "down" : "";
  return `<span class="badge ${cls}">${label} ${v > 0 ? "+" : ""}${v.toString().replace(".", ",")} %</span>`;
};

function lineChart(canvas, points, prev) {
  if (prev) prev.destroy();
  const css = getComputedStyle(document.documentElement);
  const color = css.getPropertyValue("--accent").trim();
  return new Chart(canvas, {
    type: "line",
    data: {
      labels: points.map((p) => p.day),
      datasets: [{ data: points.map((p) => p.total ?? p.price), borderColor: color, backgroundColor: color + "22",
        fill: true, tension: 0.25, pointRadius: points.length < 15 ? 3 : 0, borderWidth: 2 }],
    },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      plugins: { legend: { display: false }, tooltip: { callbacks: { label: (c) => money(c.parsed.y) } } },
      scales: {
        x: { ticks: { maxTicksLimit: 4, color: css.getPropertyValue("--muted").trim() }, grid: { display: false } },
        y: { ticks: { maxTicksLimit: 4, color: css.getPropertyValue("--muted").trim(), callback: (v) => v + " €" }, grid: { color: css.getPropertyValue("--line").trim() } },
      },
    },
  });
}

async function loadAll() {
  try {
    const [sum, col] = await Promise.all([api("/summary"), api("/collection")]);
    items = col.items;
    $("#sum-total").textContent = money(sum.total);
    $("#sum-vars").innerHTML = varBadge("1 j", sum.var1) + varBadge("7 j", sum.var7) + varBadge("30 j", sum.var30);
    $("#sum-meta").textContent = `${sum.cards} carte${sum.cards > 1 ? "s" : ""} · ${sum.unique} référence${sum.unique > 1 ? "s" : ""}` +
      (col.last_refresh ? ` · prix relevés le ${new Date(col.last_refresh).toLocaleDateString("fr-FR")}` : "");
    sumChart = lineChart($("#sum-chart"), sum.history, sumChart);
    $(".chart-box").classList.toggle("hidden", sum.history.length < 2);
    renderList();
  } catch (err) { if (err.message !== "non connecté") toast(err.message); }
}

function renderList() {
  const txt = $("#filter-text").value.trim().toLowerCase();
  const sort = $("#sort").value;
  let rows = items.filter((i) => (!filterGame || i.game === filterGame) && (!txt || i.name.toLowerCase().includes(txt) || dn(i).toLowerCase().includes(txt)));
  const cmp = {
    name: (a, b) => dn(a).localeCompare(dn(b), "fr"),
    value: (a, b) => (b.total ?? -1) - (a.total ?? -1),
    var7: (a, b) => (b.var7 ?? -999) - (a.var7 ?? -999),
    var7asc: (a, b) => (a.var7 ?? 999) - (b.var7 ?? 999),
    recent: (a, b) => b.added_at.localeCompare(a.added_at),
  }[sort];
  rows.sort(cmp);
  $("#empty").classList.toggle("hidden", items.length > 0);
  $("#list").innerHTML = rows.map(itemHtml).join("");
}

function itemHtml(i) {
  return `
    <div class="item" data-id="${i.id}">
      ${i.image ? `<img loading="lazy" src="${esc(i.image)}" alt="">` : `<div class="noimg"></div>`}
      <div class="info">
        <div class="name">${esc(dn(i))}</div>
        <div class="sub">${esc(i.set_name || GAME_LABEL[i.game])} · ${esc(i.number || "")}</div>
        <div class="sub">${esc(i.rarity || "")} · ${esc(i.condition)} · ×${i.quantity}${i.photo_recto || i.photo_verso ? " · 📷" : ""}</div>
      </div>
      <div class="right">
        <div class="price">${money(i.total)}</div>
        ${i.use_custom ? `<span class="badge">perso</span>` : varBadge("7 j", i.var7)}
      </div>
    </div>`;
}

$("#list").addEventListener("click", (e) => {
  const el = e.target.closest(".item");
  if (el) openCard(+el.dataset.id);
});
$("#filter-text").addEventListener("input", renderList);
$("#sort").addEventListener("change", renderList);
$$("#filter-game button").forEach((b) => b.addEventListener("click", () => {
  filterGame = b.dataset.g;
  $$("#filter-game button").forEach((x) => x.classList.toggle("on", x === b));
  renderList();
}));

$("#btn-refresh").addEventListener("click", async () => {
  const btn = $("#btn-refresh");
  btn.classList.add("spin");
  try {
    const r = await api("/refresh", { method: "POST" });
    toast(`Prix mis à jour (${r.ok} carte${r.ok > 1 ? "s" : ""}${r.errors ? `, ${r.errors} en échec` : ""})`);
    await loadAll();
  } catch (err) { toast(err.message); }
  btn.classList.remove("spin");
});

/* ------------------------------------------------------------- photos recto / verso de ses cartes */
const SIDES = ["recto", "verso"];
const SIDE_LABEL = { recto: "Recto", verso: "Verso" };
let lastScanBlob = null; // photo du dernier scan, proposée comme recto au moment d'ajouter la carte

/** Photo → JPEG réduit (1600 px max) : quelques centaines de Ko au lieu de plusieurs Mo, assez net pour juger l'état. */
async function toJpegBlob(file, maxSide = 1600, quality = 0.82) {
  const img = await loadImage(file);
  const k = Math.min(1, maxSide / Math.max(img.naturalWidth, img.naturalHeight));
  const c = document.createElement("canvas");
  c.width = Math.max(1, Math.round(img.naturalWidth * k));
  c.height = Math.max(1, Math.round(img.naturalHeight * k));
  c.getContext("2d").drawImage(img, 0, 0, c.width, c.height);
  URL.revokeObjectURL(img.src);
  return new Promise((resolve, reject) =>
    c.toBlob((b) => (b ? resolve(b) : reject(new Error("Photo illisible"))), "image/jpeg", quality));
}

/** Ouvre l'appareil photo et renvoie la photo prise (null si on annule). */
function pickPhoto() {
  return new Promise((resolve) => {
    const input = $("#photo-pick");
    input.onchange = () => { const f = input.files[0]; input.value = ""; resolve(f || null); };
    input.click();
  });
}

async function uploadPhoto(id, side, blob) {
  const res = await fetch(`/api/collection/${id}/photo/${side}`, {
    method: "PUT", credentials: "same-origin", headers: { "Content-Type": "image/jpeg" }, body: blob,
  });
  if (res.status === 401) { showLogin(); throw new Error("non connecté"); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || "Envoi de la photo impossible");
  return data;
}
const deletePhoto = (id, side) => api(`/collection/${id}/photo/${side}`, { method: "DELETE" });

/** Un emplacement photo : vide (« ＋ Recto ») ou rempli (miniature). `removable` ajoute la croix (écran d'ajout). */
function slotHtml(side, src, removable) {
  return src
    ? `<div class="pslot" data-side="${side}" role="button"><img src="${esc(src)}" alt="${SIDE_LABEL[side]}">` +
      `<span class="lbl">${SIDE_LABEL[side]}</span>${removable ? `<button type="button" class="x" aria-label="Retirer la photo">✕</button>` : ""}</div>`
    : `<button type="button" class="pslot empty" data-side="${side}">📷<br>＋ ${SIDE_LABEL[side]}</button>`;
}

/** Visionneuse plein écran d'une photo, avec « Reprendre » et « Supprimer ». */
function viewPhoto({ src, title, onRetake, onDelete }) {
  const d = $("#dlg-photo");
  d.innerHTML = `
    <h3 style="margin:0 0 8px">${esc(title)}</h3>
    <img class="photo-big" src="${esc(src)}" alt="${esc(title)}">
    <div class="dlg-actions">
      <button class="btn" id="p-retake">Reprendre</button>
      <button class="btn danger" id="p-del">Supprimer</button>
    </div>
    <button class="btn" id="p-close" style="margin-top:8px">Fermer</button>`;
  d.showModal();
  $("#p-close", d).onclick = () => d.close();
  $("#p-retake", d).onclick = () => { d.close(); onRetake(); };
  $("#p-del", d).onclick = () => { d.close(); onDelete(); };
}

/* ------------------------------------------------------------- fiche d'une carte */
const condOptions = (sel) => CONDITIONS.map(([c, l]) => `<option value="${c}" ${c === sel ? "selected" : ""}>${c} – ${l}</option>`).join("");

async function openCard(id) {
  const it = items.find((x) => x.id === id);
  if (!it) return;
  const dlg = $("#dlg-card");
  let qty = it.quantity;
  dlg.innerHTML = `
    <div class="head">
      ${it.image ? `<img src="${esc(it.image)}" alt="">` : ""}
      <div>
        <h2 style="margin:0 0 4px">${esc(dn(it))}</h2>
        <div class="muted small">${esc(GAME_LABEL[it.game])} · ${esc(it.set_name || "")}<br>${esc(it.number || "")} ${esc(it.rarity || "")}</div>
        <div style="margin-top:8px"><b>${money(it.price)}</b> <span class="muted small">l'unité</span>${it.use_custom ? ` <span class="badge">perso</span>` : ""}</div>
        ${it.use_custom ? `<div class="muted small">Ton prix (pas de variation)</div>` : priceNote(it.src_date, it.game)}
        ${it.use_custom ? "" : `<div class="vars" style="margin-top:6px">${varBadge("1 j", it.var1)}${varBadge("7 j", it.var7)}${varBadge("30 j", it.var30)}</div>`}
      </div>
    </div>
    <div class="dlg-chart"><canvas id="dlg-chart"></canvas></div>
    <div id="dlg-nohist" class="muted small center hidden">L'historique se construit avec un relevé de prix par jour.</div>
    <div class="row">
      <div class="qty"><button id="q-minus">−</button><b id="q-val">${qty}</b><button id="q-plus">＋</button></div>
      <select id="c-cond" class="grow" style="padding:10px;border-radius:10px;border:1px solid var(--line);background:var(--panel2);color:var(--text)">${condOptions(it.condition)}</select>
    </div>
    ${customPriceHtml(it.market_price, it.use_custom, it.custom_price)}
    <label class="field">Notes
      <input id="c-notes" type="text" value="${esc(it.notes)}" placeholder="ex. reverse, signée, à vendre…">
    </label>
    <div class="muted small">Photos de ta carte</div>
    <div class="photos" id="c-photos"></div>
    <div class="dlg-actions">
      <button class="btn danger" id="c-del">Supprimer</button>
      <button class="btn primary" id="c-save">Enregistrer</button>
    </div>
    <button class="btn" id="c-close" style="margin-top:8px">Fermer</button>`;
  dlg.showModal();

  // photos recto / verso de CET exemplaire
  const photoSrc = (s) => `/api/collection/${id}/photo/${s}?v=${it["photo_" + s]}`;
  const renderPhotos = () => {
    $("#c-photos", dlg).innerHTML = SIDES.map((s) => slotHtml(s, it["photo_" + s] ? photoSrc(s) : null, false)).join("");
  };
  const takePhoto = async (side) => {
    const file = await pickPhoto();
    if (!file) return;
    try {
      toast("Envoi de la photo…");
      const r = await uploadPhoto(id, side, await toJpegBlob(file));
      it["photo_" + side] = r.stamp;
      renderPhotos(); loadAll(); toast("Photo enregistrée ✓");
    } catch (err) { toast(err.message); }
  };
  renderPhotos();
  $("#c-photos", dlg).onclick = (e) => {
    const slot = e.target.closest(".pslot");
    if (!slot) return;
    const side = slot.dataset.side;
    if (!it["photo_" + side]) { takePhoto(side); return; }
    viewPhoto({
      src: photoSrc(side),
      title: `${dn(it)} — ${SIDE_LABEL[side]}`,
      onRetake: () => takePhoto(side),
      onDelete: async () => {
        try { await deletePhoto(id, side); it["photo_" + side] = null; renderPhotos(); loadAll(); toast("Photo supprimée"); }
        catch (err) { toast(err.message); }
      },
    });
  };

  $("#q-minus", dlg).onclick = () => { qty = Math.max(1, qty - 1); $("#q-val", dlg).textContent = qty; };
  $("#q-plus", dlg).onclick = () => { qty += 1; $("#q-val", dlg).textContent = qty; };
  $("#c-close", dlg).onclick = () => dlg.close();
  wireCustomPrice(dlg);
  $("#c-save", dlg).onclick = async () => {
    try {
      const custom = readCustomPrice(dlg);
      await api("/collection/" + id, { method: "PATCH", body: { quantity: qty, condition: $("#c-cond", dlg).value, notes: $("#c-notes", dlg).value, ...custom } });
      dlg.close(); toast("Enregistré"); loadAll();
    } catch (err) { toast(err.message); }
  };
  $("#c-del", dlg).onclick = async () => {
    if (!confirm(`Supprimer « ${dn(it)} » de la collection ?`)) return;
    await api("/collection/" + id, { method: "DELETE" });
    dlg.close(); toast("Supprimée"); loadAll();
  };
  try {
    const h = await api(`/card/${it.card_id}/history`);
    const ok = h.points.length >= 2;
    $(".dlg-chart", dlg).classList.toggle("hidden", !ok);
    $("#dlg-nohist", dlg).classList.toggle("hidden", ok);
    if (ok) dlgChart = lineChart($("#dlg-chart", dlg), h.points.map((p) => ({ day: p.day, price: p.price })), dlgChart);
  } catch (_) { /* l'historique est facultatif */ }
}

/* ------------------------------------------------------------- collectionneurs */
let memberItems = [];
let memberId = null;

async function loadMembers() {
  $("#member-view").classList.add("hidden");
  $("#members-list").classList.remove("hidden");
  try {
    const { members } = await api("/members");
    $("#members").innerHTML = members.map((m) => `
      <div class="item member" data-uid="${m.id}">
        <div class="avatar">${esc(m.username.slice(0, 1))}</div>
        <div class="info">
          <div class="name">${esc(m.username)}${m.me ? ` <span class="badge">toi${m.is_public ? "" : " · privée"}</span>` : ""}</div>
          <div class="sub">${m.cards} carte${m.cards > 1 ? "s" : ""} · ${m.unique} référence${m.unique > 1 ? "s" : ""}</div>
        </div>
        <div class="right"><div class="price">${money(m.total)}</div>${varBadge("7 j", m.var7)}</div>
      </div>`).join("") || `<p class="muted center">Personne d'autre pour l'instant.</p>`;
  } catch (err) { toast(err.message); }
}
$("#members").addEventListener("click", (e) => {
  const el = e.target.closest(".item");
  if (el) openMember(+el.dataset.uid);
});
$("#member-back").addEventListener("click", loadMembers);

async function openMember(uid) {
  try {
    const r = await api("/members/" + uid);
    memberId = uid;
    memberItems = r.items;
    $("#mv-name").textContent = `Collection de ${r.member.username}`;
    $("#mv-total").textContent = money(r.summary.total);
    $("#mv-vars").innerHTML = varBadge("1 j", r.summary.var1) + varBadge("7 j", r.summary.var7) + varBadge("30 j", r.summary.var30);
    $("#mv-meta").textContent = `${r.summary.cards} carte${r.summary.cards > 1 ? "s" : ""} · ${r.summary.unique} référence${r.summary.unique > 1 ? "s" : ""}`;
    $("#mv-filter").value = "";
    renderMember();
    $("#members-list").classList.add("hidden");
    $("#member-view").classList.remove("hidden");
    window.scrollTo(0, 0);
  } catch (err) { toast(err.message); }
}
function renderMember() {
  const txt = $("#mv-filter").value.trim().toLowerCase();
  const rows = memberItems.filter((i) => !txt || dn(i).toLowerCase().includes(txt) || i.name.toLowerCase().includes(txt))
    .sort((a, b) => (b.total ?? -1) - (a.total ?? -1));
  $("#mv-list").innerHTML = rows.map(itemHtml).join("") || `<p class="muted center">Aucune carte.</p>`;
}
$("#mv-filter").addEventListener("input", renderMember);
$("#mv-list").addEventListener("click", (e) => {
  const el = e.target.closest(".item");
  if (!el) return;
  if (me && memberId === me.id) { openCard(+el.dataset.id); return; }
  viewCard(memberItems.find((x) => x.id === +el.dataset.id));
});

/** Fiche en lecture seule d'une carte d'un autre membre. */
async function viewCard(it) {
  if (!it) return;
  const dlg = $("#dlg-card");
  const photos = SIDES.filter((s) => it["photo_" + s]);
  dlg.innerHTML = `
    <div class="head">
      ${it.image ? `<img src="${esc(it.image)}" alt="">` : ""}
      <div>
        <h2 style="margin:0 0 4px">${esc(dn(it))}</h2>
        <div class="muted small">${esc(GAME_LABEL[it.game])} · ${esc(it.set_name || "")}<br>${esc(it.number || "")} ${esc(it.rarity || "")}</div>
        <div style="margin-top:8px"><b>${money(it.price)}</b> <span class="muted small">l'unité · ${esc(it.condition)} · ×${it.quantity}</span></div>
        ${it.use_custom ? `<div class="muted small">Prix fixé par son propriétaire</div>` : priceNote(it.src_date, it.game)}
        ${it.use_custom ? "" : `<div class="vars" style="margin-top:6px">${varBadge("1 j", it.var1)}${varBadge("7 j", it.var7)}${varBadge("30 j", it.var30)}</div>`}
      </div>
    </div>
    <div class="dlg-chart"><canvas id="dlg-chart"></canvas></div>
    ${photos.length ? `<div class="photos">${photos.map((s) => slotHtml(s, `/api/collection/${it.id}/photo/${s}?v=${it["photo_" + s]}`, false)).join("")}</div>` : ""}
    <button class="btn" id="c-close" style="margin-top:8px">Fermer</button>`;
  dlg.showModal();
  $("#c-close", dlg).onclick = () => dlg.close();
  try {
    const h = await api(`/card/${it.card_id}/history`);
    const ok = h.points.length >= 2;
    $(".dlg-chart", dlg).classList.toggle("hidden", !ok);
    if (ok) dlgChart = lineChart($("#dlg-chart", dlg), h.points.map((p) => ({ day: p.day, price: p.price })), dlgChart);
  } catch (_) { /* facultatif */ }
}

/* ------------------------------------------------------------- ajout */
$$("#add-game button").forEach((b) => b.addEventListener("click", () => {
  addGame = b.dataset.g;
  $$("#add-game button").forEach((x) => x.classList.toggle("on", x === b));
  $("#results").innerHTML = "";
}));

async function runSearch() {
  const q = $("#q-name").value.trim();
  const number = $("#q-number").value.trim();
  if (!q && !number) { toast("Indique un nom ou un numéro"); return; }
  const box = $("#results");
  box.innerHTML = `<p class="muted center">Recherche…</p>`;
  try {
    const params = new URLSearchParams({ game: addGame, q, number });
    let { results } = await api("/search?" + params);
    // le numéro lu par la photo est souvent faux : si rien ne sort, on retente sur le nom seul
    if (!results.length && q && number) {
      $("#q-number").value = "";
      ({ results } = await api("/search?" + new URLSearchParams({ game: addGame, q })));
    }
    window._results = results;
    box.innerHTML = results.length
      ? results.map((c, i) => `
        <div class="item" data-i="${i}">
          ${c.image ? `<img loading="lazy" src="${esc(c.image)}" alt="">` : `<div class="noimg"></div>`}
          <div class="info">
            <div class="name">${esc(dn(c))}</div>
            <div class="sub">${esc(c.set_name || "")}</div>
            <div class="sub">${esc(c.number || "")} · ${esc(c.rarity || "")}</div>
          </div>
          <div class="right"><div class="price">${money(c.price)}</div></div>
        </div>`).join("")
      : `<p class="muted center">Aucun résultat. Essaie avec moins de lettres ou sans numéro.</p>`;
  } catch (err) { box.innerHTML = `<p class="err center">${esc(err.message)}</p>`; }
}
$("#btn-search").addEventListener("click", runSearch);
["#q-name", "#q-number"].forEach((s) => $(s).addEventListener("keydown", (e) => { if (e.key === "Enter") runSearch(); }));

$("#results").addEventListener("click", (e) => {
  const el = e.target.closest(".item");
  if (!el) return;
  const c = window._results[+el.dataset.i];
  const dlg = $("#dlg-add");
  let qty = 1;
  dlg.innerHTML = `
    <div class="head">
      ${c.image ? `<img src="${esc(c.image)}" alt="">` : ""}
      <div>
        <h2 style="margin:0 0 4px">${esc(dn(c))}</h2>
        <div class="muted small">${esc(c.set_name || "")}<br>${esc(c.number || "")} ${esc(c.rarity || "")}</div>
        <div style="margin-top:8px"><b>${money(c.price)}</b></div>
        ${priceNote(c.src_date, c.game)}
      </div>
    </div>
    <div class="row">
      <div class="qty"><button id="a-minus">−</button><b id="a-val">1</b><button id="a-plus">＋</button></div>
      <select id="a-cond" class="grow" style="padding:10px;border-radius:10px;border:1px solid var(--line);background:var(--panel2);color:var(--text)">${condOptions("NM")}</select>
    </div>
    ${customPriceHtml(c.price, false, null)}
    <div class="muted small">Photos de ta carte (facultatif)</div>
    <div class="photos" id="a-photos"></div>
    <div class="dlg-actions">
      <button class="btn" id="a-cancel">Annuler</button>
      <button class="btn primary" id="a-ok">Ajouter</button>
    </div>`;
  dlg.showModal();

  // photos à enregistrer avec la carte : la photo du scan est proposée comme recto, le verso se prend ici
  const pending = { recto: null, verso: null };
  const urls = { recto: null, verso: null };
  const setPending = (side, blob) => {
    if (urls[side]) URL.revokeObjectURL(urls[side]);
    pending[side] = blob;
    urls[side] = blob ? URL.createObjectURL(blob) : null;
  };
  const renderSlots = () => {
    $("#a-photos", dlg).innerHTML = SIDES.map((s) => slotHtml(s, urls[s], true)).join("");
  };
  setPending("recto", lastScanBlob);
  renderSlots();
  dlg.onclose = () => SIDES.forEach((s) => { if (urls[s]) URL.revokeObjectURL(urls[s]); });
  $("#a-photos", dlg).onclick = async (e) => {
    const slot = e.target.closest(".pslot");
    if (!slot) return;
    const side = slot.dataset.side;
    if (e.target.closest(".x")) { setPending(side, null); renderSlots(); return; }
    const file = await pickPhoto();
    if (!file) return;
    try { setPending(side, await toJpegBlob(file)); renderSlots(); } catch (err) { toast(err.message); }
  };

  $("#a-minus", dlg).onclick = () => { qty = Math.max(1, qty - 1); $("#a-val", dlg).textContent = qty; };
  $("#a-plus", dlg).onclick = () => { qty += 1; $("#a-val", dlg).textContent = qty; };
  $("#a-cancel", dlg).onclick = () => dlg.close();
  wireCustomPrice(dlg);
  $("#a-ok", dlg).onclick = async () => {
    const okBtn = $("#a-ok", dlg);
    if (okBtn.disabled) return; // évite un double appui pendant l'envoi des photos
    let custom;
    try { custom = readCustomPrice(dlg); } catch (err) { toast(err.message); return; }
    okBtn.disabled = true;
    try {
      const res = await api("/collection", { method: "POST", body: { ...c, quantity: qty, condition: $("#a-cond", dlg).value, ...custom } });
      let photoFailed = false;
      for (const s of SIDES) {
        if (!pending[s]) continue;
        try { await uploadPhoto(res.entry_id, s, pending[s]); } catch (_) { photoFailed = true; }
      }
      dlg.close();
      lastScanBlob = null;
      toast(photoFailed ? `${dn(c)} ajoutée, mais une photo n'a pas pu être enregistrée` : `${dn(c)} ajoutée ✓`);
      // prêt pour la carte suivante
      $("#results").innerHTML = ""; $("#q-name").value = ""; $("#q-number").value = "";
      $("#preview").classList.add("hidden"); $("#ocr-status").textContent = "";
      loadAll();
    } catch (err) { okBtn.disabled = false; toast(err.message); }
  };
});

/* ------------------------------------------------------------- photo + OCR */
function loadImage(file) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = reject;
    img.src = URL.createObjectURL(file);
  });
}

// <detect>
/** Repère la carte dans la photo : la carte est la grande zone « texturée » (illustration, texte) sur un fond plutôt uni.
 *  gray : niveaux de gris d'une miniature (w × h). Renvoie {x, y, w, h} en fractions de l'image, ou null en cas de doute
 *  (la photo entière est alors lue, comme avant). */
function detectCardBox(gray, w, h) {
  const cs = Math.max(4, Math.round(Math.max(w, h) / 40)); // côté d'une case
  const cw = Math.floor(w / cs), ch = Math.floor(h / cs);
  if (cw < 6 || ch < 6) return null;

  // 1) densité de contours dans chaque case
  const dens = new Float32Array(cw * ch);
  let maxD = 0;
  for (let cy = 0; cy < ch; cy++) {
    for (let cx = 0; cx < cw; cx++) {
      let sum = 0, n = 0;
      for (let y = cy * cs; y < (cy + 1) * cs - 1; y++) {
        for (let x = cx * cs; x < (cx + 1) * cs - 1; x++) {
          const i = y * w + x;
          sum += Math.abs(gray[i + 1] - gray[i]) + Math.abs(gray[i + w] - gray[i]);
          n++;
        }
      }
      const d = sum / n;
      dens[cy * cw + cx] = d;
      if (d > maxD) maxD = d;
    }
  }
  if (maxD < 4) return null; // photo sans aucun détail

  // 2) seuil d'Otsu : sépare « fond » et « texturé »
  const BINS = 64, hist = new Array(BINS).fill(0);
  for (const d of dens) hist[Math.min(BINS - 1, Math.floor((d / maxD) * (BINS - 1)))]++;
  const total = dens.length;
  let sumAll = 0;
  for (let i = 0; i < BINS; i++) sumAll += i * hist[i];
  let wB = 0, sumB = 0, best = -1, bestBin = 0;
  for (let i = 0; i < BINS; i++) {
    wB += hist[i];
    if (!wB) continue;
    const wF = total - wB;
    if (!wF) break;
    sumB += i * hist[i];
    const between = wB * wF * (sumB / wB - (sumAll - sumB) / wF) ** 2;
    if (between > best) { best = between; bestBin = i; }
  }
  // Otsu seul serait trop sévère (il ne verrait que l'illustration) : on le plafonne par rapport au bruit du fond
  // (densité médiane), pour garder aussi les cases du contour de la carte et ses aplats de couleur.
  const sorted = Float32Array.from(dens).sort();
  const median = sorted[sorted.length >> 1];
  const thr = Math.max(3, Math.min(((bestBin + 1) / (BINS - 1)) * maxD, 2.5 * median + 2));
  const mask = Uint8Array.from(dens, (d) => (d >= thr ? 1 : 0));

  // 3) on relie les cases voisines (aplats de couleur dans l'illustration), puis on garde la plus grande zone
  const dil = new Uint8Array(cw * ch);
  for (let cy = 0; cy < ch; cy++) {
    for (let cx = 0; cx < cw; cx++) {
      if (!mask[cy * cw + cx]) continue;
      for (let dy = -1; dy <= 1; dy++) {
        for (let dx = -1; dx <= 1; dx++) {
          const nx = cx + dx, ny = cy + dy;
          if (nx >= 0 && ny >= 0 && nx < cw && ny < ch) dil[ny * cw + nx] = 1;
        }
      }
    }
  }
  const seen = new Uint8Array(cw * ch);
  let bestComp = null;
  for (let s = 0; s < cw * ch; s++) {
    if (!dil[s] || seen[s]) continue;
    const stack = [s];
    seen[s] = 1;
    const comp = { size: 0, x0: cw, x1: -1, y0: ch, y1: -1 };
    while (stack.length) {
      const idx = stack.pop();
      const cx = idx % cw, cy = (idx - cx) / cw;
      comp.size++;
      if (mask[idx]) { // la boîte se calcule sur les vraies cases texturées, sans la marge de la dilatation
        comp.x0 = Math.min(comp.x0, cx); comp.x1 = Math.max(comp.x1, cx);
        comp.y0 = Math.min(comp.y0, cy); comp.y1 = Math.max(comp.y1, cy);
      }
      for (const [dx, dy] of [[1, 0], [-1, 0], [0, 1], [0, -1]]) {
        const nx = cx + dx, ny = cy + dy;
        if (nx < 0 || ny < 0 || nx >= cw || ny >= ch) continue;
        const ni = ny * cw + nx;
        if (dil[ni] && !seen[ni]) { seen[ni] = 1; stack.push(ni); }
      }
    }
    if (comp.x1 >= 0 && (!bestComp || comp.size > bestComp.size)) bestComp = comp;
  }
  if (!bestComp) return null;

  // 4) boîte en fractions, avec une demi-case de marge, puis contrôle de vraisemblance (carte = rectangle portrait)
  const pad = 0.5;
  const x0 = Math.max(0, (bestComp.x0 - pad) * cs) / w, x1 = Math.min(w, (bestComp.x1 + 1 + pad) * cs) / w;
  const y0 = Math.max(0, (bestComp.y0 - pad) * cs) / h, y1 = Math.min(h, (bestComp.y1 + 1 + pad) * cs) / h;
  const bw = x1 - x0, bh = y1 - y0;
  const area = bw * bh, aspect = (bw * w) / (bh * h);
  if (area < 0.1 || area > 0.97 || aspect < 0.4 || aspect > 0.95) return null;
  return { x: x0, y: y0, w: bw, h: bh };
}
// </detect>

/** Découpe une zone (fractions 0-1 de la photo), niveaux de gris contrastés, remise à une taille lisible pour l'OCR. */
function cropToCanvas(img, x, y, w, h, targetW = 1500) {
  const sw = img.naturalWidth * w, sh = img.naturalHeight * h;
  const scale = Math.max(0.2, Math.min(2.5, targetW / sw));
  const c = document.createElement("canvas");
  c.width = Math.max(1, Math.round(sw * scale)); c.height = Math.max(1, Math.round(sh * scale));
  const ctx = c.getContext("2d", { willReadFrequently: true });
  ctx.drawImage(img, img.naturalWidth * x, img.naturalHeight * y, sw, sh, 0, 0, c.width, c.height);
  const d = ctx.getImageData(0, 0, c.width, c.height);
  for (let i = 0; i < d.data.length; i += 4) {
    const g = 0.299 * d.data[i] + 0.587 * d.data[i + 1] + 0.114 * d.data[i + 2];
    const v = Math.max(0, Math.min(255, (g - 128) * 1.5 + 128));
    d.data[i] = d.data[i + 1] = d.data[i + 2] = v;
  }
  ctx.putImageData(d, 0, 0);
  return c;
}

// mots qui ne sont jamais le nom d'une carte (cadre, copyright, règles, types d'attaques…)
const NOISE = /\b(pv|hp|niveau|lv|lvl|base|stade|stage|phase|vmax|vstar|gx|ex|de base|baby)\b/gi;
const NOT_A_NAME = /illus|nintendo|creatures|gamefreak|game freak|pok[eé]mon|words|wizards|coast|konami|kazuki|takahashi|studio|weakness|r[eé]sistance|resistance|retreat|retraite|faiblesse|dresseur|trainer|attaque|d[eé]g[aâ]ts|damage|talent|ability|r[eé]serv|longueur|poids|taille|\b(?:atk|def|rare|common|length|weight|energy|[eé]nergie)\b/i;

/** Choisit le nom parmi les lignes lues : la plus haute de la carte, courte, lisible, sans mot parasite.
 *  Mieux vaut ne rien proposer qu'une phrase absurde : la recherche par numéro prend alors le relais. */
function guessName(lines) {
  const cands = lines
    .map((l) => ({ y: l.y, conf: l.conf, text: l.text.replace(/[^A-Za-zÀ-ÿ'’\- ]/g, " ").replace(NOISE, " ").replace(/\s+/g, " ").trim() }))
    .filter((l) => {
      const letters = l.text.replace(/[^A-Za-zÀ-ÿ]/g, "").length;
      return l.conf >= 55 && letters >= 3 && letters <= 22 && l.text.split(" ").length <= 3 && !NOT_A_NAME.test(l.text);
    })
    .sort((a, b) => a.y - b.y);
  return cands[0] ? cands[0].text : "";
}

// lettres que l'OCR prend souvent pour des chiffres, dans un numéro : O→0, I/l/|→1, S→5, B→8, Z→2
const DIGIT_FIX = { O: "0", o: "0", I: "1", l: "1", L: "1", "|": "1", S: "5", B: "8", Z: "2" };
const fixDigits = (s) => s.replace(/[OoIlL|SBZ]/g, (c) => DIGIT_FIX[c]);

function guessNumber(text, game) {
  const t = text.replace(/[–—]/g, "-");
  if (game === "yugioh") {
    // ex. LOB-FR005 (l'étranger peut être « LOB FR005 » ou « LOB-EN0O5 » à la lecture)
    const re = /(?:^|[^A-Z0-9])([A-Z0-9]{2,5})\s?-\s?([A-Z]{2})?\s?([0-9OIlLSBZ]{3})(?![0-9A-Za-z])/g;
    for (const m of t.toUpperCase().matchAll(re)) {
      const digits = fixDigits(m[3]);
      if (/^\d{3}$/.test(digits)) return `${m[1]}-${m[2] || ""}${digits}`;
    }
    return "";
  }
  if (game === "onepiece") {
    // ex. OP01-001, ST10-005, EB01-012, P-012 (le 0 et le O se confondent souvent)
    const re = /(?:^|[^A-Z0-9])((?:[O0]P|ST|EB|PRB)[0-9OIl]{2}|P)\s?-\s?([0-9OIlLSBZ]{3})(?![0-9A-Za-z])/g;
    for (const m of t.toUpperCase().matchAll(re)) {
      const head = m[1].replace(/^0P/, "OP").replace(/(?<=^(?:OP|ST|EB|PRB))[0-9OIL]+/, (x) => fixDigits(x));
      const digits = fixDigits(m[2]);
      if (/^\d{3}$/.test(digits)) return `${head}-${digits}`;
    }
    return "";
  }
  if (game === "magic") {
    // cadre moderne : « 0123 R » puis « MKM • FR » ; cadre 2015-2022 : « 123/280 R » puis « MKM • FR »
    const up = t.toUpperCase();
    const num = up.match(/(?:^|[^0-9])([0-9OIlSB]{1,4})(?:\s*\/\s*[0-9OIlSB]{1,4})?\s+[CURMSLT](?![A-Z0-9])/);
    const set = up.match(/(?:^|[^A-Z0-9])([A-Z0-9]{3,5})\s*[•·*.\-]\s*(?:EN|FR|DE|IT|ES|JA|PT)(?![A-Z])/);
    if (num && set) {
      const n = fixDigits(num[1]).replace(/^0+(?=\d)/, "");
      if (/^\d+$/.test(n)) return `${set[1]} ${n}`;
    }
    return "";
  }
  // Pokémon : 58/102 (le « / » est parfois lu « | » ou « \ »)
  const re = /(?:^|[^0-9A-Za-z])([0-9OoIlSB]{1,3})\s*[\/\\|]\s*([0-9OoIlSB]{2,3})(?![0-9A-Za-z])/g;
  for (const m of t.matchAll(re)) {
    const a = fixDigits(m[1]), b = fixDigits(m[2]);
    if (/^\d{1,3}$/.test(a) && /^\d{2,3}$/.test(b) && +b >= 10) return `${a}/${b}`;
  }
  return "";
}

let ocrWorker = null;
async function getWorker() {
  if (!ocrWorker) {
    ocrWorker = await Tesseract.createWorker("fra+eng");
    // 11 = « texte épars » : bien adapté à un bandeau qui mêle nom, PV, symboles…
    await ocrWorker.setParameters({ tessedit_pageseg_mode: "11", preserve_interword_spaces: "1" });
  }
  return ocrWorker;
}

const toLines = (data) =>
  data.lines && data.lines.length
    ? data.lines.map((l) => ({ text: l.text, conf: l.confidence, y: l.bbox ? l.bbox.y0 : 0 }))
    : data.text.split(/\n+/).map((t, i) => ({ text: t, conf: 100, y: i }));

/** Miniature de la photo en niveaux de gris → position de la carte (ou null). */
function findCard(img) {
  const k = 240 / Math.max(img.naturalWidth, img.naturalHeight);
  const c = document.createElement("canvas");
  c.width = Math.max(1, Math.round(img.naturalWidth * k));
  c.height = Math.max(1, Math.round(img.naturalHeight * k));
  const ctx = c.getContext("2d", { willReadFrequently: true });
  ctx.drawImage(img, 0, 0, c.width, c.height);
  const px = ctx.getImageData(0, 0, c.width, c.height).data;
  const gray = new Uint8ClampedArray(c.width * c.height);
  for (let i = 0, j = 0; i < px.length; i += 4, j++) gray[j] = 0.299 * px[i] + 0.587 * px[i + 1] + 0.114 * px[i + 2];
  return detectCardBox(gray, c.width, c.height);
}

/** Envoie la photo au serveur, qui la fait reconnaître par Claude. Renvoie null si la carte n'est pas reconnue. */
async function identifyByAI(file) {
  const blob = await toJpegBlob(file, 1280, 0.85);
  const r = await fetch("/api/identify", {
    method: "POST", credentials: "same-origin", headers: { "Content-Type": "image/jpeg" }, body: blob,
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || "Reconnaissance impossible");
  if (!GAME_LABEL[data.game] || (!data.name && !data.number)) return null;
  if (data.game !== addGame) { // la carte est d'un autre jeu que l'onglet choisi : on bascule
    addGame = data.game;
    $$("#add-game button").forEach((x) => x.classList.toggle("on", x.dataset.g === addGame));
  }
  return {
    name: data.name, number: data.number, found: true, thumb: null,
    raw: `Reconnue par l'IA : ${GAME_LABEL[data.game]} · « ${data.name} » · ${data.number || "numéro illisible"} (confiance : ${data.confidence})`,
  };
}

/** Lit nom et numéro : on recadre sur la carte, puis on lit le haut (nom) et le bas (numéro) séparément, en haute résolution. */
async function readCard(img, game, onStep) {
  const worker = await getWorker();
  const found = findCard(img);
  const box = found || { x: 0, y: 0, w: 1, h: 1 };
  const zone = (y0, y1) => cropToCanvas(img, box.x, box.y + box.h * y0, box.w, box.h * (y1 - y0));
  const raw = [];

  onStep("Lecture du nom…");
  const top = await worker.recognize(zone(0, 0.2));
  raw.push("[haut de la carte] " + top.data.text.trim());
  let name = guessName(toLines(top.data));
  if (!name) { // 2e essai : lecture ligne par ligne
    await worker.setParameters({ tessedit_pageseg_mode: "6" });
    const again = await worker.recognize(zone(0, 0.2));
    await worker.setParameters({ tessedit_pageseg_mode: "11" });
    raw.push("[haut, 2e essai] " + again.data.text.trim());
    name = guessName(toLines(again.data));
  }

  onStep("Lecture du numéro…");
  let number = "";
  // Pokémon : numéro en bas de carte. Yu-Gi-Oh! : code sous l'illustration (parfois en bas).
  const zones = {
    yugioh: [[0.5, 0.82], [0.8, 1]],
    magic: [[0.88, 1], [0.8, 1]], // bas à gauche : numéro, rareté, extension
    onepiece: [[0.86, 1], [0.78, 1]], // bas à droite : OP01-001
  }[game] || [[0.8, 1], [0.7, 1]];
  for (const [a, b] of zones) {
    const r = await worker.recognize(zone(a, b));
    raw.push(`[${Math.round(a * 100)}-${Math.round(b * 100)} % de la carte] ` + r.data.text.trim());
    number = guessNumber(r.data.text, game);
    if (number) break;
  }
  if (!number && game === "pokemon") { // dernier essai : seulement des chiffres et « / »
    await worker.setParameters({ tessedit_char_whitelist: "0123456789/" });
    const r = await worker.recognize(zone(0.8, 1));
    await worker.setParameters({ tessedit_char_whitelist: "" });
    raw.push("[chiffres seuls] " + r.data.text.trim());
    number = guessNumber(r.data.text, game);
  }

  // vignette de ce qui a été repéré, pour que tu voies si la carte a bien été trouvée
  let thumb = null;
  if (found) {
    const full = cropToCanvas(img, box.x, box.y, box.w, box.h, 360);
    thumb = full.toDataURL("image/jpeg", 0.7);
  }
  return { name, number, raw: raw.join("\n"), found: !!found, thumb };
}

$("#photo").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  e.target.value = "";
  if (!file) return;
  const status = $("#ocr-status");
  // on garde une version réduite de cette photo : elle sera proposée comme recto de la carte à l'ajout
  lastScanBlob = null;
  toJpegBlob(file).then((b) => { lastScanBlob = b; }).catch(() => {});
  try {
    const img = await loadImage(file);
    $("#preview").src = img.src;
    $("#preview").classList.remove("hidden");
    $("#results").innerHTML = "";
    let res = null;
    if (sessionInfo.ai) {
      // reconnaissance par l'IA (serveur) : trouve aussi le jeu tout seul. En cas d'échec, lecture de texte locale.
      status.textContent = "Reconnaissance de la carte…";
      res = await identifyByAI(file).catch((err) => { console.warn(err); return null; });
    }
    if (!res) {
      status.textContent = "Lecture de la carte… (la première fois, le module met quelques secondes à se charger)";
      res = await readCard(img, addGame, (s) => { status.textContent = s; });
    }
    if (res.thumb) $("#preview").src = res.thumb; // montre la carte repérée
    $("#q-name").value = res.name;
    $("#q-number").value = res.number;
    $("#ocr-raw").textContent = res.raw.startsWith("Reconnue par l'IA") ? res.raw
      : `Carte repérée dans la photo : ${res.found ? "oui" : "non (photo entière lue)"}\n${res.raw}`;
    $("#ocr-debug").classList.remove("hidden");
    status.textContent = res.name || res.number
      ? "Vérifie le nom et le numéro, puis choisis la bonne carte ci-dessous."
      : "Lecture impossible : tape le nom à la main.";
    if (res.name || res.number) runSearch(); else $("#q-name").focus();
  } catch (err) {
    status.textContent = "Lecture automatique indisponible (" + err.message + ") — tape le nom à la main.";
  }
});

/* ------------------------------------------------------------- démarrage */
if ("serviceWorker" in navigator) navigator.serviceWorker.register("sw.js").catch(() => {});

(async function boot() {
  try {
    const s = await fetch("/api/session", { credentials: "same-origin" }).then((r) => r.json());
    sessionInfo = s;
    me = s.user;
    if (s.ok) { showApp(); loadAll(); } else showLogin();
  } catch (_) { showApp(); toast("Serveur injoignable"); }
})();
