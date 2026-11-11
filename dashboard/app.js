import { formatAge, linePoints, projectTrack } from "./lib.js";

const config = {
  apiBaseUrl: "http://127.0.0.1:8080",
  refreshSeconds: 5,
  historyPoints: 120,
  ...(window.DASHBOARD_CONFIG ?? {}),
};
const SVG_NS = "http://www.w3.org/2000/svg";

const SIGNALS = [
  { key: "speed", label: "Speed", unit: "km/h", digits: 0 },
  { key: "rpm", label: "Engine speed", unit: "rpm", digits: 0 },
  { key: "coolant_temperature", label: "Coolant", unit: "°C", digits: 1 },
  { key: "battery_voltage", label: "Battery", unit: "V", digits: 2 },
  { key: "fuel_level", label: "Fuel", unit: "%", digits: 0 },
];

const $ = (id) => document.getElementById(id);
let selected = null;
let inflight = 0; // guards against out-of-order responses after switching vehicle

async function api(path) {
  const response = await fetch(`${config.apiBaseUrl}${path}`, { headers: { Accept: "application/json" } });
  if (!response.ok) throw new Error(`${response.status} ${response.statusText} for ${path}`);
  return response.json();
}

/** Build elements with textContent only: API data is never interpreted as HTML. */
function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(props)) {
    if (name === "class") node.className = value;
    else node.setAttribute(name, value);
  }
  node.append(...children);
  return node;
}

function setStatus(ok, message) {
  const status = $("status");
  status.textContent = message;
  status.className = `pill ${ok ? "ok" : "danger"}`;
  const banner = $("error");
  banner.hidden = ok;
  if (!ok) banner.textContent = message;
}

function renderFleet(vehicles) {
  const list = $("vehicle-list");
  list.replaceChildren(
    ...vehicles.map((v) => {
      const button = el(
        "button",
        { type: "button", "aria-current": String(v.vehicle_id === selected) },
        el("span", {}, v.vehicle_id),
        el("span", { class: `pill ${v.online ? "ok" : "off"}` }, v.online ? "online" : "offline"),
      );
      button.addEventListener("click", () => selectVehicle(v.vehicle_id));
      return el("li", {}, button);
    }),
  );
  $("empty-fleet").hidden = vehicles.length > 0;
  if (selected === null && vehicles.length > 0) selectVehicle(vehicles[0].vehicle_id);
}

function renderCards(latest) {
  $("cards").replaceChildren(
    ...SIGNALS.map((s) =>
      el(
        "div",
        { class: "card" },
        el("div", { class: "label" }, s.label),
        el("div", { class: "value" }, Number(latest[s.key]).toFixed(s.digits), el("span", { class: "unit" }, s.unit)),
      ),
    ),
    el("div", { class: "card" }, el("div", { class: "label" }, "Engine"), el("div", { class: "value" }, latest.engine_status)),
  );
}

function renderCharts(history) {
  const width = 400;
  const height = 70;
  $("charts").replaceChildren(
    ...SIGNALS.map((s) => {
      const { points, min, max } = linePoints(history.map((h) => h[s.key]), width, height);
      const svg = document.createElementNS(SVG_NS, "svg");
      svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
      svg.setAttribute("preserveAspectRatio", "none");
      svg.setAttribute("role", "img");
      svg.setAttribute("aria-label", `${s.label} history`);
      const line = document.createElementNS(SVG_NS, "polyline");
      line.setAttribute("points", points);
      svg.append(line);
      return el(
        "div",
        { class: "chart" },
        el("div", { class: "head" }, el("span", {}, `${s.label} (${s.unit})`), el("span", {}, `${min.toFixed(s.digits)} – ${max.toFixed(s.digits)}`)),
        svg,
      );
    }),
  );
}

function renderMap(history) {
  const svg = $("map");
  const projected = projectTrack(history, 320, 240);
  $("track-count").textContent = String(projected.length);
  svg.replaceChildren();
  if (projected.length === 0) return;
  const path = document.createElementNS(SVG_NS, "polyline");
  path.setAttribute("class", "track");
  path.setAttribute("points", projected.map((p) => `${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(" "));
  const last = projected[projected.length - 1];
  const dot = document.createElementNS(SVG_NS, "circle");
  dot.setAttribute("class", "here");
  dot.setAttribute("cx", last.x.toFixed(1));
  dot.setAttribute("cy", last.y.toFixed(1));
  dot.setAttribute("r", "5");
  svg.append(path, dot);
}

function renderDetail(detail, history) {
  $("placeholder").hidden = true;
  $("detail").hidden = false;
  $("vehicle-title").textContent = detail.vehicle_id;
  const badge = $("online-badge");
  badge.textContent = detail.online ? "online" : "offline";
  badge.className = `pill ${detail.online ? "ok" : "off"}`;
  const dtc = $("dtc-badge");
  dtc.hidden = !detail.active_dtc;
  dtc.textContent = detail.active_dtc ? `DTC ${detail.active_dtc}` : "";
  $("last-seen").textContent = `last data ${formatAge(detail.last_seen)} · gateway ${detail.gateway_id}`;
  renderCards(detail.latest);
  renderCharts(history);
  renderMap(history);
}

async function loadSelected() {
  if (selected === null) return;
  const ticket = ++inflight;
  const id = encodeURIComponent(selected);
  const [detail, page] = await Promise.all([
    api(`/vehicles/${id}`),
    api(`/vehicles/${id}/telemetry?order=desc&limit=${config.historyPoints}`),
  ]);
  if (ticket !== inflight) return; // a newer selection superseded this response
  renderDetail(detail, page.items.slice().reverse()); // oldest -> newest for the charts
}

function selectVehicle(id) {
  selected = id;
  for (const button of document.querySelectorAll("#vehicle-list button")) {
    button.setAttribute("aria-current", String(button.firstChild.textContent === id));
  }
  refresh();
}

async function refresh() {
  try {
    renderFleet(await api("/vehicles"));
    await loadSelected();
    setStatus(true, `live · updated ${new Date().toLocaleTimeString()}`);
  } catch (error) {
    setStatus(false, `API unreachable or failing: ${error.message}`);
  }
}

refresh();
setInterval(refresh, config.refreshSeconds * 1000);
