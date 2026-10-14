import { api } from "./api.js";

const $ = (id) => document.getElementById(id);
const form = $("filters");
const PAGE_SIZE = 25;

const state = { window: "daily", offset: 0, total: 0, status: null, urlSource: "" };

// ---------------------------------------------------------------- helpers

/** Build a DOM node. Text goes in via textContent: feed data is never parsed as HTML. */
function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === "class") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value);
  }
  node.append(...children.filter((c) => c != null));
  return node;
}

function safeUrl(url) {
  try {
    const parsed = new URL(url);
    return parsed.protocol === "http:" || parsed.protocol === "https:" ? parsed.href : null;
  } catch {
    return null;
  }
}

function formatDate(iso) {
  return iso ? new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" }) : null;
}

function showNotice(message, isError = false) {
  const box = $("notice");
  box.hidden = !message;
  box.textContent = message ?? "";
  box.classList.toggle("error", isError);
}

// ---------------------------------------------------------------- filters <-> URL

function filterQuery() {
  const data = new FormData(form);
  const query = [["window", state.window], ["sort", data.get("sort")]];
  if (data.get("source")) query.push(["source", data.get("source")]);
  if (data.get("remote")) query.push(["remote", data.get("remote")]);
  if (Number(data.get("min_score")) > 0) query.push(["min_score", data.get("min_score")]);
  if (data.get("bookmarked")) query.push(["bookmarked", "true"]);
  return query;
}

function readUrlIntoForm() {
  const params = new URLSearchParams(window.location.search);
  state.window = params.get("window") === "all" ? "all" : "daily";
  state.urlSource = params.get("source") ?? ""; // the option list only exists once /status has loaded
  for (const name of ["remote", "sort", "min_score"]) {
    if (params.has(name)) form.elements[name].value = params.get(name);
  }
  form.elements.bookmarked.checked = params.get("bookmarked") === "true";
  state.offset = Math.max(0, Number(params.get("offset")) || 0);
}

function writeUrl() {
  const query = filterQuery();
  if (state.offset) query.push(["offset", String(state.offset)]);
  history.replaceState(null, "", `?${new URLSearchParams(query)}`);
}

function syncFormLabels() {
  for (const button of document.querySelectorAll("[data-window]")) {
    button.setAttribute("aria-pressed", String(button.dataset.window === state.window));
  }
  const min = Number(form.elements.min_score.value);
  $("min-score-out").textContent = min > 0 ? min.toFixed(2) : "default";
}

// ---------------------------------------------------------------- rendering

function renderProfile(status) {
  const { profile } = status;
  if (!profile) {
    $("profile").textContent = "No CV yet. Upload a PDF to start matching.";
    return;
  }
  const role = profile.roles[0] ?? "Candidate";
  const years = profile.years_experience != null ? `${Math.round(profile.years_experience)} yrs` : null;
  const parts = [role, profile.seniority, years, profile.skills.slice(0, 6).join(", ")].filter(Boolean);
  $("profile").textContent = parts.join(" · ");
}

function renderSources(status) {
  const select = form.elements.source;
  const current = select.value || state.urlSource;
  state.urlSource = "";
  select.replaceChildren(el("option", { value: "" }, "Any"),
    ...status.sources.map((name) => el("option", { value: name }, name)));
  select.value = status.sources.includes(current) ? current : "";
}

function renderRun(status) {
  const run = status.last_run;
  const parts = [];
  if (run) {
    parts.push(`Last sync ${new Date(run.started_at).toLocaleString()} (${run.status}): ${run.jobs_new} new, ${run.jobs_matched_count} matched.`);
    if (run.error) parts.push(run.error);
  } else {
    parts.push("No sync yet.");
  }
  $("footnote").textContent =
    `${parts.join(" ")} Only jobs scored on their full description are ranked; threshold ${status.similarity_threshold}.`;
  $("sync-btn").disabled = status.sync_in_progress || !status.profile;
  $("sync-btn").textContent = status.sync_in_progress ? "Syncing…" : "Sync now";
}

function sourceClass(text) {
  if (text.startsWith("ok")) return "src-ok";
  return text.startsWith("partial") ? "src-partial" : "src-error";
}

function renderSyncResult(result) {
  const panel = $("sync-result");
  panel.hidden = false;
  panel.replaceChildren(
    el("h2", {}, `Sync ${result.status}: ${result.fetched} fetched, ${result.new} new, ${result.matched} matched, ` +
      `${result.enriched} enriched (${result.duration_seconds}s)`),
    el("ul", {}, ...Object.entries(result.sources).map(([name, text]) =>
      el("li", { class: sourceClass(text) }, `${name}: ${text}`))),
  );
}

function jobCard(job, rank) {
  const href = safeUrl(job.url);
  const title = href
    ? el("a", { href, target: "_blank", rel: "noopener noreferrer" }, job.title)
    : job.title;
  const percent = Math.round(job.relevance_score * 100);
  const meta = [
    job.company && el("span", {}, job.company),
    job.location && el("span", {}, job.location),
    job.is_remote && el("span", { class: "tag" }, "remote"),
    el("span", { class: "tag" }, job.source),
    job.published_at && el("span", {}, `posted ${formatDate(job.published_at)}`),
  ];

  const bookmark = el("button", {
    type: "button",
    "aria-pressed": String(job.is_bookmarked),
    onclick: () => toggleBookmark(job, bookmark),
  }, job.is_bookmarked ? "★ Saved" : "☆ Save");
  const dismiss = el("button", { type: "button", onclick: () => dismissJob(job, card) }, "Dismiss");

  const card = el("li", { class: "job" },
    el("div", {},
      el("div", { class: "rank" }, `#${rank}`),
      el("div", { class: "score" }, `${percent}%`),
      el("div", { class: "bar" }, el("span", { style: `width:${percent}%` }))),
    el("div", {},
      el("h3", {}, title),
      el("div", { class: "meta" }, ...meta.filter(Boolean)),
      job.snippet ? el("p", { class: "snippet" }, job.snippet) : null),
    el("div", { class: "row-actions" }, bookmark, dismiss));
  return card;
}

function renderJobs(page) {
  state.total = page.total;
  const list = $("jobs");
  if (page.items.length === 0) {
    const text = state.status?.profile
      ? "No matches for these filters. Try Sync now, or lower the minimum score."
      : "Upload your CV, then run a sync to see matches.";
    list.replaceChildren(el("li", { class: "empty" }, text));
  } else {
    list.replaceChildren(...page.items.map((job, i) => jobCard(job, page.offset + i + 1)));
  }

  const label = page.window === "daily" ? "in today's digest" : "stored matches";
  $("summary").textContent = `${page.total} ${label}`;

  const paged = page.total > PAGE_SIZE;
  $("pager").hidden = !paged;
  if (paged) {
    const first = page.offset + 1;
    $("range").textContent = `${first}–${page.offset + page.items.length} of ${page.total}`;
    $("prev").disabled = page.offset === 0;
    $("next").disabled = page.offset + PAGE_SIZE >= page.total;
  }
}

// ---------------------------------------------------------------- actions

async function loadJobs() {
  syncFormLabels();
  writeUrl();
  try {
    const query = [...filterQuery(), ["limit", String(PAGE_SIZE)], ["offset", String(state.offset)]];
    renderJobs(await api.jobs(query));
  } catch (error) {
    showNotice(error.message, true);
  }
}

async function loadStatus() {
  state.status = await api.status();
  renderProfile(state.status);
  renderSources(state.status);
  renderRun(state.status);
  return state.status;
}

function resetAndLoad() {
  state.offset = 0;
  return loadJobs();
}

async function toggleBookmark(job, button) {
  try {
    const updated = await api.patch(job.id, { is_bookmarked: !job.is_bookmarked });
    job.is_bookmarked = updated.is_bookmarked;
    button.setAttribute("aria-pressed", String(job.is_bookmarked));
    button.textContent = job.is_bookmarked ? "★ Saved" : "☆ Save";
  } catch (error) {
    showNotice(error.message, true);
  }
}

async function dismissJob(job, card) {
  try {
    await api.patch(job.id, { is_dismissed: true });
    card.remove();
    state.total -= 1;
    $("summary").textContent = $("summary").textContent.replace(/^\d+/, String(state.total));
  } catch (error) {
    showNotice(error.message, true);
  }
}

async function runSync() {
  showNotice(null);
  $("sync-btn").disabled = true;
  $("sync-btn").textContent = "Syncing…";
  try {
    renderSyncResult(await api.sync());
  } catch (error) {
    showNotice(error.message, true);
  }
  await loadStatus();
  await resetAndLoad();
}

async function uploadCv(file) {
  showNotice(null);
  try {
    await api.upload(file);
    showNotice("CV uploaded. Run a sync to match jobs against it.");
    await loadStatus();
  } catch (error) {
    showNotice(error.message, true);
  }
}

/** Poll while a sync started elsewhere (scheduler, CLI, another tab) is running. */
function watchSyncInProgress() {
  setInterval(async () => {
    if (!state.status?.sync_in_progress) return;
    const before = state.status;
    await loadStatus().catch(() => {});
    if (before.sync_in_progress && !state.status.sync_in_progress) await loadJobs();
  }, 3000);
}

// ---------------------------------------------------------------- wiring

let sliderTimer;
form.addEventListener("input", (event) => {
  if (event.target.name !== "min_score") return;
  syncFormLabels();
  clearTimeout(sliderTimer);
  sliderTimer = setTimeout(resetAndLoad, 250);
});
form.addEventListener("change", (event) => {
  if (event.target.name !== "min_score") resetAndLoad();
});
form.addEventListener("submit", (event) => event.preventDefault());

for (const button of document.querySelectorAll("[data-window]")) {
  button.addEventListener("click", () => {
    state.window = button.dataset.window;
    resetAndLoad();
  });
}

$("prev").addEventListener("click", () => {
  state.offset = Math.max(0, state.offset - PAGE_SIZE);
  loadJobs();
});
$("next").addEventListener("click", () => {
  state.offset += PAGE_SIZE;
  loadJobs();
});
$("sync-btn").addEventListener("click", runSync);
$("cv-input").addEventListener("change", (event) => {
  const [file] = event.target.files;
  event.target.value = "";
  if (file) uploadCv(file);
});

readUrlIntoForm();
loadStatus().then(loadJobs).catch((error) => showNotice(error.message, true));
watchSyncInProgress();
