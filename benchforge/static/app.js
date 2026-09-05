const $ = selector => document.querySelector(selector);

const ACTIVE = new Set([
  "QUEUED", "PREPARING", "BASELINE", "MODEL", "PATCH", "EVALUATING"
]);
const EXCLUDED = new Set([
  "INFRA_ERROR", "BASELINE_INVALID", "INTERRUPTED", "CANCELLED"
]);
const STATUS_LABELS = {
  QUEUED: "Queued",
  PREPARING: "Preparing",
  BASELINE: "Checking baseline",
  MODEL: "Generating patch",
  PATCH: "Validating patch",
  EVALUATING: "Running tests",
  TASK_RESOLVED: "Resolved",
  TEST_FAILED: "Tests failed",
  PATCH_INVALID: "Invalid patch",
  PATCH_APPLY_FAILED: "Patch rejected",
  MODEL_FAILED: "Model failed",
  INFRA_ERROR: "Infrastructure error",
  BASELINE_INVALID: "Invalid baseline",
  INTERRUPTED: "Interrupted",
  CANCELLED: "Cancelled",
};

const state = {
  runs: [],
  filter: "all",
  search: "",
  detail: null,
  detailRecord: null,
  detailVersion: null,
  tableMarkup: null,
  comparisonMarkup: null,
  refreshing: false,
  refreshAgain: false,
  loaded: false,
  health: null,
  catalog: null,
  selectedBenchmarkId: "tiny-v0.1",
  view: "evaluations",
};

let toastTimer;
let refreshTimer;

function escapeHTML(value) {
  return String(value ?? "").replace(/[&<>"']/g, character => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[character]));
}

function duration(value) {
  if (value == null || !Number.isFinite(Number(value))) return "—";
  const seconds = Number(value);
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const rounded = Math.round(seconds);
  return `${Math.floor(rounded / 60)}m ${rounded % 60}s`;
}

function shortDate(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleString([], {
    month: "short", day: "numeric",
    hour: "2-digit", minute: "2-digit",
  });
}

function timeOnly(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleTimeString([], {
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  });
}

function statusClass(status) {
  if (status === "TASK_RESOLVED") return "good";
  if (ACTIVE.has(status)) return "active";
  if (EXCLUDED.has(status)) return "warn";
  return "bad";
}

function badge(status) {
  const label = STATUS_LABELS[status] || String(status).replaceAll("_", " ");
  return `<span class="status ${statusClass(status)}">${escapeHTML(label)}</span>`;
}

function eligible(run) {
  return !ACTIVE.has(run.status) && !EXCLUDED.has(run.status);
}

function toast(message) {
  $("#toast").textContent = message;
  $("#toast").classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => $("#toast").classList.remove("show"), 4000);
}

function setHealthItem(selector, ok, title) {
  const element = $(selector);
  if (!element) return;
  element.dataset.ok = ok === "warn" ? "warn" : ok ? "true" : "false";
  if (title) element.title = title;
}

function renderHealth(health) {
  if (!health) return;
  state.health = health;
  setHealthItem(
    "#health-git",
    health.git_installed,
    health.git_installed ? "Git is available" : "Git was not found on PATH"
  );
  setHealthItem(
    "#health-docker",
    health.docker_daemon,
    health.docker_daemon
      ? "Docker daemon is reachable"
      : health.docker_installed
        ? "Docker is installed but the daemon is not reachable"
        : "Docker was not found on PATH"
  );
  setHealthItem(
    "#health-image",
    health.image_present,
    health.image_present
      ? (health.image_id || health.image)
      : `${health.image} is not present locally`
  );
  setHealthItem(
    "#health-provider",
    health.provider_configured ? true : "warn",
    health.provider_configured
      ? "At least one provider model is configured"
      : "Set PROVIDER_MODEL or ANTHROPIC_MODEL to enable a provider"
  );
}

function applyAdapterAvailability(health) {
  const providers = health?.providers || {};
  const chat = $("#option-chat");
  const anthropic = $("#option-anthropic");
  if (chat) chat.disabled = !providers["chat-completions"];
  if (anthropic) anthropic.disabled = !providers["anthropic-messages"];

  const selected = $("#adapter").value;
  if (selected === "chat-completions" && chat?.disabled) {
    $("#adapter").value = "mock-fixed";
    updateAdapterNote();
  }
  if (selected === "anthropic-messages" && anthropic?.disabled) {
    $("#adapter").value = "mock-fixed";
    updateAdapterNote();
  }
}

function selectedBenchmark() {
  const items = state.catalog?.benchmarks || [];
  return items.find(item => item.id === state.selectedBenchmarkId) || items[0] || null;
}

function populateBenchmarkSelect() {
  const select = $("#benchmark-select");
  if (!select || !state.catalog) return;
  const current = state.selectedBenchmarkId;
  select.innerHTML = (state.catalog.benchmarks || []).map(item => {
    const task = item.task || {};
    const label = `${item.id} — ${task.title || item.id}`;
    return `<option value="${escapeHTML(item.id)}">${escapeHTML(label)}</option>`;
  }).join("");
  if ([...select.options].some(option => option.value === current)) {
    select.value = current;
  }
}

function renderCatalog(data) {
  if (!data?.benchmarks) return;
  state.catalog = data;
  if (!data.benchmarks.some(item => item.id === state.selectedBenchmarkId)) {
    state.selectedBenchmarkId = data.benchmarks[0].id;
  }
  populateBenchmarkSelect();
  const note = $("#dialog-benchmark-note");
  if (note) {
    note.textContent = `${data.benchmarks.length} bundled tasks · hidden tests stay on the host`;
  }

  const markup = data.benchmarks.map(item => {
    const task = item.task || {};
    const expected = item.expected || {};
    const spec = [
      expected.input && `input ${expected.input}`,
      expected.raises && `raises ${expected.raises}`,
      expected.returns && `returns ${expected.returns}`,
    ].filter(Boolean).join(" · ");
    return `<article class="suite-card">
      <div class="kicker">${escapeHTML([task.language, task.category].filter(Boolean).join(" / ").toUpperCase())}</div>
      <h3>${escapeHTML(task.title || item.id)}</h3>
      <p>${escapeHTML(item.summary || task.description || "")}</p>
      <div class="facts">
        <span>${escapeHTML(item.id)}</span>
        <span>edit ${escapeHTML((item.allowed_files || []).join(", "))}</span>
        <span>${escapeHTML(String(item.timeout_seconds))}s</span>
      </div>
      ${spec ? `<div class="code-spec"><code>${escapeHTML(spec)}</code></div>` : ""}
      <footer>
        <span class="standard-tag">${escapeHTML(item.standard || "application").toUpperCase()}</span>
        <button type="button" class="text-button" data-run-benchmark="${escapeHTML(item.id)}">
          Run task <span aria-hidden="true">→</span>
        </button>
      </footer>
    </article>`;
  }).join("");
  const grid = $("#suite-grid");
  if (grid) grid.innerHTML = markup;
}

function runActionButtons(run) {
  const id = escapeHTML(run.id);
  const short = escapeHTML(run.id.slice(0, 8));
  const inspect = `<button
        class="row-open"
        data-inspect="${id}"
        aria-label="Inspect run ${short}"
        title="Inspect run"
      >↗</button>`;

  if (ACTIVE.has(run.status)) {
    return `<div class="row-actions">
      <button class="row-action" data-action="cancel" data-id="${id}">Cancel</button>
      ${inspect}
    </div>`;
  }

  return `<div class="row-actions">
      <button class="row-action" data-action="retry" data-id="${id}">Retry</button>
      <button class="row-action" data-action="delete" data-id="${id}">Delete</button>
      ${inspect}
    </div>`;
}

async function api(url, options = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 20000);

  try {
    const response = await fetch(url, {
      ...options,
      cache: "no-store",
      signal: controller.signal,
    });

    if (!response.ok) {
      let message = `Request failed (${response.status}).`;
      try {
        const body = await response.json();
        if (typeof body.detail === "string") {
          message = body.detail;
        } else if (Array.isArray(body.detail)) {
          message = body.detail.map(item => item.msg).join(" ");
        }
      } catch {}
      throw new Error(message);
    }

    if (response.status === 204) return null;

    return await response.json();
  } catch (error) {
    if (error.name === "AbortError") {
      throw new Error("The request timed out. Check the run list before submitting again.");
    }
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

function renderMetrics() {
  const runs = state.runs;
  const mocks = runs.filter(run => run.demo).length;
  const providers = runs.filter(run => !run.demo);
  const scored = providers.filter(eligible);
  const resolved = scored.filter(run => run.resolved).length;
  const completed = runs.filter(
    run => !ACTIVE.has(run.status) && run.duration != null
  );

  $("#metric-total").textContent = runs.length;
  $("#metric-total-note").textContent =
    `${providers.length} provider · ${mocks} mock trials`;

  $("#metric-rate").textContent = scored.length
    ? `${Math.round(100 * resolved / scored.length)}%`
    : "—";
  $("#metric-rate-note").textContent = scored.length
    ? `${resolved}/${scored.length} eligible · mocks excluded`
    : "No eligible provider trials";

  $("#metric-time").textContent = completed.length
    ? duration(completed.reduce((sum, run) => sum + Number(run.duration), 0) / completed.length)
    : "—";

  $("#metric-active").textContent =
    runs.filter(run => ACTIVE.has(run.status)).length;
}

function comparisonGroup(title, groups, mock = false) {
  if (!groups.length) return "";

  return `<section class="comparison-group ${mock ? "mock" : ""}">
    <div class="group-label">
      <span>${escapeHTML(title)}</span>
      <span>${mock ? "HARNESS CHECKS ONLY" : "SYNTHETIC FIXTURE"}</span>
    </div>
    ${groups.map(group => {
      const rate = Math.round(100 * group.resolved / group.count);
      return `<div class="comparison-row">
        <div class="comparison-title">
          <strong>${escapeHTML(group.model)}</strong>
          <span class="comparison-score">
            ${rate}% <small>${group.resolved}/${group.count}</small>
          </span>
        </div>
        <div
          class="bar-track"
          role="img"
          aria-label="${escapeHTML(group.model)}: ${group.resolved} of ${group.count} trials resolved"
        >
          <div class="bar-fill" style="width:${rate}%"></div>
        </div>
      </div>`;
    }).join("")}
  </section>`;
}

function renderComparison() {
  const groups = new Map();

  state.runs.filter(eligible).forEach(run => {
    const key = JSON.stringify([run.adapter, run.model, Boolean(run.demo)]);
    if (!groups.has(key)) {
      groups.set(key, {
        model: run.model,
        demo: Boolean(run.demo),
        count: 0,
        resolved: 0,
      });
    }
    const group = groups.get(key);
    group.count += 1;
    group.resolved += Number(Boolean(run.resolved));
  });

  const values = [...groups.values()].sort(
    (a, b) => String(a.model).localeCompare(String(b.model))
  );

  const providers = values.filter(group => !group.demo);
  const mocks = values.filter(group => group.demo);

  $("#comparison-count").textContent =
    `${values.length} ${values.length === 1 ? "model" : "models"}`;

  const markup = values.length
    ? comparisonGroup("PROVIDER TRIALS", providers) +
      comparisonGroup("MOCK ADAPTERS", mocks, true)
    : `<div class="chart-empty">
        <div class="empty-rule" aria-hidden="true"></div>
        <h3>Nothing to compare yet.</h3>
        <p>Completed, eligible trials will appear here. Provider results and mock checks stay separate.</p>
      </div>`;

  if (markup !== state.comparisonMarkup) {
    $("#comparison-content").innerHTML = markup;
    state.comparisonMarkup = markup;
  }
}

function visibleRuns() {
  return state.runs.filter(run => {
    const searchable = `${run.model} ${run.id} ${run.task_id} ${run.benchmark_id || ""}`.toLowerCase();
    if (!searchable.includes(state.search)) return false;

    if (state.filter === "active") return ACTIVE.has(run.status);
    if (state.filter === "resolved") return Boolean(run.resolved);
    if (state.filter === "failed") {
      return !ACTIVE.has(run.status) && !run.resolved;
    }
    return true;
  });
}

function renderTable() {
  const runs = visibleRuns();

  $("#run-count").textContent = state.runs.length;
  $("#table-count").textContent = `${runs.length} of ${state.runs.length} runs`;
  $("#empty-state").hidden = runs.length > 0;

  if (!runs.length) {
    const hasHistory = state.runs.length > 0;
    $("#empty-state h3").textContent = hasHistory
      ? "No matching evaluations."
      : "No evaluations yet.";
    $("#empty-state p").textContent = hasHistory
      ? "Try another model, run ID, or status filter."
      : "Start with a mock run to verify the harness, then connect a provider.";
    $("#empty-run").hidden = hasHistory;
  }

  const markup = runs.map(run => `<tr>
    <td>
      <div class="run-id">${escapeHTML(run.id.slice(0, 8))}<span class="trial-label">#${escapeHTML(run.trial)}</span></div>
      <div class="task-subtitle">${
        run.parent_run_id
          ? `retry of ${escapeHTML(run.parent_run_id.slice(0, 8))}`
          : escapeHTML(run.adapter || "")
      }</div>
    </td>
    <td>
      <div class="model-name" title="${escapeHTML(run.benchmark_id || "")}">${escapeHTML(run.benchmark_id || "—")}</div>
      <div class="task-subtitle">${escapeHTML(run.task_id || "")}</div>
    </td>
    <td>
      <div class="model-name" title="${escapeHTML(run.model)}">${escapeHTML(run.model)}</div>
      <div class="task-subtitle">${run.demo ? "Mock / harness check" : "Provider trial"}</div>
    </td>
    <td>${badge(run.status)}</td>
    <td class="numeric">${duration(run.duration)}</td>
    <td>
      <span class="patch-add">+${escapeHTML(run.lines_added)}</span>
      <span class="patch-remove">−${escapeHTML(run.lines_removed)}</span>
    </td>
    <td>${escapeHTML(shortDate(run.created_at))}</td>
    <td>${runActionButtons(run)}</td>
  </tr>`).join("");

  // Avoid replacing focused buttons every polling interval.
  if (markup !== state.tableMarkup) {
    const focused = document.activeElement;
    const focusInspect = focused?.dataset?.inspect;
    const focusAction = focused?.dataset?.action;
    const focusId = focused?.dataset?.id;
    $("#runs-body").innerHTML = markup;
    state.tableMarkup = markup;

    if (focusInspect) {
      document.querySelector(`[data-inspect="${CSS.escape(focusInspect)}"]`)
        ?.focus({ preventScroll: true });
    } else if (focusAction && focusId) {
      document.querySelector(`[data-action="${CSS.escape(focusAction)}"][data-id="${CSS.escape(focusId)}"]`)
        ?.focus({ preventScroll: true });
    }
  }
}

function formatPatch(patch) {
  if (!patch) return '<span class="muted">No source changes recorded.</span>';

  return patch.split("\n").map(line => {
    let className = "";
    if (line.startsWith("+") && !line.startsWith("+++")) className = "diff-add";
    else if (line.startsWith("-") && !line.startsWith("---")) className = "diff-remove";
    else if (line.startsWith("@@")) className = "diff-hunk";
    return `<span class="${className}">${escapeHTML(line)}</span>`;
  }).join("\n");
}

function renderDetail(run) {
  const version = JSON.stringify(run);
  if (version === state.detailVersion) return;

  const opened = new Set(
    [...document.querySelectorAll("#detail-content details[open]")]
      .map(element => element.dataset.key)
  );
  const scrollTop = $("#detail-dialog").scrollTop;

  state.detailRecord = run;
  state.detailVersion = version;
  $("#detail-title").textContent = `Run ${run.id.slice(0, 8)}`;

  const commands = run.commands || [];
  const stats = [
    ["Model", run.model],
    ["Wall time", duration(run.duration)],
    ["Baseline", run.baseline_status],
    ["Patch", run.patch_status],
    ["Tests", run.test_status],
    ["Provider cost", "Not available"],
  ];

  const metadata = {
    id: run.id,
    experiment_id: run.experiment_id,
    trial: run.trial,
    benchmark_id: run.benchmark_id,
    benchmark_hash: run.benchmark_hash,
    repository: run.repository,
    base_commit: run.base_commit,
    model_config: run.model_config,
    returned_model: run.returned_model,
    model_latency: run.model_latency,
    token_usage: run.token_usage,
    environment: run.environment,
  };

  const artifacts = [];
  if (run.finished_at) artifacts.push("result.json");
  if (run.model_status === "SUCCEEDED") artifacts.push("model-response.txt");
  if (
    ["APPLIED", "EMPTY", "APPLY_FAILED"].includes(run.patch_status) ||
    commands.some(command => command.label === "patch check")
  ) {
    artifacts.push("patch.diff");
  }

  $("#detail-content").innerHTML = `
    <div class="detail-status-line">
      ${badge(run.status)}
      <span class="inspector-actions">
        ${ACTIVE.has(run.status)
          ? `<button class="row-action" data-action="cancel" data-id="${escapeHTML(run.id)}">Cancel run</button>`
          : `<button class="row-action" data-action="retry" data-id="${escapeHTML(run.id)}">Retry</button>
             <button class="row-action" data-action="delete" data-id="${escapeHTML(run.id)}">Delete</button>`}
        <span class="subtle">Trial ${escapeHTML(run.trial)} · ${escapeHTML(shortDate(run.created_at))}</span>
      </span>
    </div>

    <p class="detail-context">
      ${run.demo
        ? "Mock smoke test. This run measures harness behavior, not model coding quality."
        : "Provider evaluation on a bundled synthetic fixture."}
    </p>

    <div class="detail-summary">
      ${stats.map(([label, value]) => `
        <div class="detail-stat">
          <small>${escapeHTML(label)}</small>
          <strong>${escapeHTML(value)}</strong>
        </div>
      `).join("")}
    </div>

    ${(run.errors || []).map(error => `
      <div class="error-box">${escapeHTML(error)}</div>
    `).join("")}

    <section class="detail-section">
      <h3>Execution timeline</h3>
      <ol class="timeline">
        ${(run.events || []).map(event => `
          <li class="timeline-item">
            <time class="timeline-time" datetime="${escapeHTML(event.at)}">${escapeHTML(timeOnly(event.at))}</time>
            <div class="timeline-body">
              <strong>${escapeHTML(event.phase)}</strong>
              <p>${escapeHTML(event.message)}</p>
            </div>
          </li>
        `).join("")}
      </ol>
    </section>

    <section class="detail-section">
      <div class="detail-section-title">
        <h3>
          Repository patch
          <span class="patch-add">+${escapeHTML(run.lines_added)}</span>
          <span class="patch-remove">−${escapeHTML(run.lines_removed)}</span>
        </h3>
        ${run.patch ? '<button class="copy-button" data-copy="patch">Copy patch</button>' : ""}
      </div>
      <pre>${formatPatch(run.patch)}</pre>
    </section>

    <section class="detail-section">
      <h3>Commands & test output <span class="muted">(${commands.length})</span></h3>
      ${commands.map((command, index) => {
        const key = `command-${index}`;
        const output = (command.stdout || "") +
          (command.stderr ? "\n[stderr]\n" + command.stderr : "");

        return `<details class="command" data-key="${key}" ${opened.has(key) ? "open" : ""}>
          <summary>${escapeHTML(command.label)} <span class="muted">/ exit ${escapeHTML(command.exit_code ?? "—")}${command.timeout ? " / timed out" : ""}</span></summary>
          <div class="command-meta">
            <span>Duration ${duration(command.duration)}</span>
            <span>${command.spawn_error ? "Process did not start" : "Process launched"}</span>
          </div>
          <code class="command-line">${escapeHTML((command.command || []).join(" "))}</code>
          ${command.output_truncated ? '<p class="subtle">Output was truncated at the capture limit.</p>' : ""}
          <pre>${escapeHTML(output) || "(no output)"}</pre>
        </details>`;
      }).join("") || '<p class="subtle">No commands have completed yet.</p>'}
    </section>

    <section class="detail-section">
      <h3>Reproducibility</h3>
      <details class="metadata" data-key="metadata" ${opened.has("metadata") ? "open" : ""}>
        <summary>Environment, model configuration & fixture identity</summary>
        <pre>${escapeHTML(JSON.stringify(metadata, null, 2))}</pre>
      </details>
    </section>

    <section class="detail-section">
      <h3>Artifacts</h3>
      <div class="artifact-links">
        ${artifacts.map(name => `
          <a class="button secondary compact"
             href="/api/runs/${encodeURIComponent(run.id)}/artifacts/${name}">
            ${escapeHTML(name)} <span aria-hidden="true">↓</span>
          </a>
        `).join("") || '<span class="subtle">No artifacts available yet.</span>'}
      </div>
      <p class="subtle">Result JSON is written at completion. Early failures may not produce every artifact.</p>
    </section>
  `;

  $("#detail-dialog").scrollTop = scrollTop;
}

async function loadDetail(id) {
  const run = await api(`/api/runs/${encodeURIComponent(id)}`);
  // Ignore responses for an inspector that was closed or switched.
  if (state.detail !== id || !$("#detail-dialog").open) return;
  renderDetail(run);
}

async function refresh() {
  clearTimeout(refreshTimer);

  if (state.refreshing) {
    state.refreshAgain = true;
    return;
  }

  if (document.hidden) {
    refreshTimer = setTimeout(refresh, 2000);
    return;
  }

  state.refreshing = true;

  try {
    const [runs, health] = await Promise.all([
      api("/api/runs"),
      api("/api/health"),
    ]);
    state.runs = runs;
    state.loaded = true;
    renderHealth(health);
    applyAdapterAvailability(health);
    renderMetrics();
    renderComparison();
    renderTable();

    $("#refresh-label").innerHTML =
      '<i class="live-dot" aria-hidden="true"></i>Synced · refreshes every 2s';

    if (state.detail && $("#detail-dialog").open) {
      try {
        await loadDetail(state.detail);
      } catch {
        $("#refresh-label").textContent = "Inspector sync delayed · retrying";
      }
    }
  } catch {
    $("#refresh-label").textContent = "Connection unavailable · retrying";

    if (!state.loaded) {
      $("#empty-state h3").textContent = "Workspace unavailable.";
      $("#empty-state p").textContent =
        "Check that the BenchForge API is running. This page will reconnect automatically.";
      $("#empty-run").hidden = true;
    }
  } finally {
    state.refreshing = false;
    const delay = state.refreshAgain ? 0 : 2000;
    state.refreshAgain = false;
    refreshTimer = setTimeout(refresh, delay);
  }
}

function updateAdapterNote() {
  const adapter = $("#adapter").value;
  if (adapter === "chat-completions") {
    $("#adapter-note").textContent =
      "POST /chat/completions to PROVIDER_BASE_URL (OpenAI, Groq, Gemini OpenAI-compat, Ollama, OpenRouter, Mistral, Together, Azure, vLLM, …). Uses PROVIDER_MODEL and optional PROVIDER_API_KEY. Hidden tests are not sent.";
  } else if (adapter === "anthropic-messages") {
    $("#adapter-note").textContent =
      "POST /v1/messages to ANTHROPIC_BASE_URL. Uses ANTHROPIC_MODEL and optional ANTHROPIC_API_KEY. Hidden tests are not sent.";
  } else {
    $("#adapter-note").textContent =
      "Mock adapters are deterministic smoke tests. They verify the evaluation harness, not a model's coding ability.";
  }
}

async function openRunDialog(benchmarkId) {
  if (benchmarkId) state.selectedBenchmarkId = benchmarkId;
  populateBenchmarkSelect();
  $("#form-error").hidden = true;
  $("#form-error").textContent = "";
  $("#docker-info").textContent = "Checking local configuration…";
  $("#submit-run").disabled = true;
  updateAdapterNote();

  if (!$("#run-dialog").open) $("#run-dialog").showModal();

  try {
    const health = await api("/api/health?refresh=true");
    renderHealth(health);
    applyAdapterAvailability(health);

    if (!health.git_installed || !health.docker_installed) {
      $("#docker-info").textContent =
        "Git or Docker was not found. Install both before running.";
    } else if (!health.docker_daemon) {
      $("#docker-info").textContent =
        "Docker is installed but the daemon is not reachable. Start Docker Desktop.";
    } else if (!health.image_present) {
      $("#docker-info").textContent =
        `${health.image} is not present locally. Pull the image before running.`;
    } else {
      const digest = (health.image_digests || [])[0] || health.image_id || health.image;
      $("#docker-info").textContent = digest;
      $("#submit-run").disabled = false;
    }
  } catch {
    $("#docker-info").textContent = "Cannot reach the API.";
  }
}

["#new-run", "#empty-run"].forEach(selector => {
  $(selector).addEventListener("click", () => openRunDialog());
});

["#close-dialog", "#cancel-dialog"].forEach(selector => {
  $(selector).addEventListener("click", () => $("#run-dialog").close());
});

$("#adapter").addEventListener("change", updateAdapterNote);

$("#run-form").addEventListener("submit", async event => {
  event.preventDefault();
  if (!$("#run-form").reportValidity()) return;

  const button = $("#submit-run");
  button.disabled = true;
  button.textContent = "Queuing…";
  $("#form-error").hidden = true;

  try {
    const adapter = $("#adapter").value;
    if (adapter === "chat-completions" && !state.health?.providers?.["chat-completions"]) {
      throw new Error("Set PROVIDER_MODEL (or OPENAI_MODEL) before using chat completions.");
    }
    if (adapter === "anthropic-messages" && !state.health?.providers?.["anthropic-messages"]) {
      throw new Error("Set ANTHROPIC_MODEL before using Anthropic Messages.");
    }

    const result = await api("/api/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        adapter: $("#adapter").value,
        benchmark_id: $("#benchmark-select").value,
        trials: Number($("#trials").value),
      }),
    });

    $("#run-dialog").close();
    setView("evaluations");
    const count = result.run_ids.length;
    toast(`${count} evaluation${count === 1 ? "" : "s"} queued.`);
    refresh();
    $("#runs").scrollIntoView({
      behavior: matchMedia("(prefers-reduced-motion: reduce)").matches
        ? "instant"
        : "smooth",
      block: "start",
    });
  } catch (error) {
    $("#form-error").textContent = error.message;
    $("#form-error").hidden = false;
  } finally {
    button.disabled = false;
    button.innerHTML = 'Start evaluation <span aria-hidden="true">→</span>';
  }
});

document.querySelectorAll(".filter-tab").forEach(button => {
  button.addEventListener("click", () => {
    state.filter = button.dataset.filter;

    document.querySelectorAll(".filter-tab").forEach(tab => {
      const selected = tab === button;
      tab.classList.toggle("active", selected);
      tab.setAttribute("aria-pressed", String(selected));
    });

    renderTable();
  });
});

$("#search").addEventListener("input", event => {
  state.search = event.target.value.trim().toLowerCase();
  renderTable();
});

async function handleRunAction(action, id, button) {
  if (!action || !id) return;
  if (button) button.disabled = true;

  try {
    if (action === "cancel") {
      await api(`/api/runs/${encodeURIComponent(id)}/cancel`, { method: "POST" });
      toast("Cancellation requested.");
    } else if (action === "retry") {
      const result = await api(
        `/api/runs/${encodeURIComponent(id)}/retry`,
        { method: "POST" },
      );
      toast(`${result.run_ids.length} evaluation queued.`);
    } else if (action === "delete") {
      if (!window.confirm("Delete this run and its artifacts?")) return;
      await api(`/api/runs/${encodeURIComponent(id)}`, { method: "DELETE" });
      if (state.detail === id) $("#detail-dialog").close();
      toast("Run deleted.");
    } else {
      return;
    }
    refresh();
  } catch (error) {
    toast(error.message);
  } finally {
    if (button) button.disabled = false;
  }
}

$("#runs-body").addEventListener("click", async event => {
  const action = event.target.closest("[data-action]");
  if (action) {
    await handleRunAction(action.dataset.action, action.dataset.id, action);
    return;
  }

  const button = event.target.closest("[data-inspect]");
  if (!button) return;

  const id = button.dataset.inspect;
  state.detail = id;
  state.detailRecord = null;
  state.detailVersion = null;
  $("#detail-title").textContent = `Run ${id.slice(0, 8)}`;
  $("#detail-content").innerHTML =
    '<p class="loading-state">Loading evaluation evidence…</p>';

  if (!$("#detail-dialog").open) $("#detail-dialog").showModal();
  $("#detail-dialog").scrollTop = 0;

  try {
    await loadDetail(id);
  } catch (error) {
    if (state.detail !== id) return;
    $("#detail-content").innerHTML =
      `<div class="error-box">${escapeHTML(error.message)} The inspector will retry automatically.</div>`;
  }
});

$("#close-detail").addEventListener("click", () => {
  $("#detail-dialog").close();
});

$("#detail-dialog").addEventListener("close", () => {
  state.detail = null;
  state.detailRecord = null;
  state.detailVersion = null;
});

$("#detail-content").addEventListener("click", async event => {
  const action = event.target.closest("[data-action]");
  if (action) {
    await handleRunAction(action.dataset.action, action.dataset.id, action);
    return;
  }

  const button = event.target.closest("[data-copy]");
  if (!button || !state.detailRecord?.patch) return;

  try {
    await navigator.clipboard.writeText(state.detailRecord.patch);
    toast("Patch copied.");
  } catch {
    toast("Clipboard access unavailable. Download patch.diff instead.");
  }
});

document.querySelectorAll("dialog").forEach(dialog => {
  dialog.addEventListener("click", event => {
    if (event.target !== dialog) return;
    const rect = dialog.getBoundingClientRect();
    const inside = (
      event.clientX >= rect.left &&
      event.clientX <= rect.right &&
      event.clientY >= rect.top &&
      event.clientY <= rect.bottom
    );
    if (!inside) dialog.close();
  });
});

function setView(view) {
  const next = view === "benchmarks" ? "benchmarks" : "evaluations";
  state.view = next;
  document.body.dataset.view = next;
  const hash = next === "benchmarks" ? "#benchmarks" : "#evaluations";
  if (location.hash !== hash) {
    history.replaceState(null, "", hash);
  }
  document.querySelectorAll(".main-nav a[data-view]").forEach(link => {
    link.classList.toggle("selected", link.dataset.view === next);
  });
  $("#page-eyebrow").textContent =
    next === "benchmarks" ? "WORKSPACE / BENCHMARKS" : "WORKSPACE / EVALUATIONS";
  $("#page-title").textContent = next === "benchmarks" ? "Benchmarks" : "Evaluations";
  $("#page-lead").textContent = next === "benchmarks"
    ? "The starter suite that the harness and dashboard are measured against."
    : "Run a coding task. Inspect the patch. Verify the outcome.";
  $("#view-evaluations").hidden = next !== "evaluations";
  $("#view-benchmarks").hidden = next !== "benchmarks";
}

function updateNavigation() {
  const hash = location.hash;
  if (hash === "#benchmarks" || hash === "#benchmark") {
    setView("benchmarks");
    return;
  }
  setView("evaluations");
}

window.addEventListener("hashchange", updateNavigation);

$("#suite-grid").addEventListener("click", event => {
  const button = event.target.closest("[data-run-benchmark]");
  if (!button) return;
  openRunDialog(button.dataset.runBenchmark);
});

$("#benchmark-select").addEventListener("change", event => {
  state.selectedBenchmarkId = event.target.value;
});

$("#health-refresh").addEventListener("click", async () => {
  $("#health-refresh").disabled = true;
  try {
    const health = await api("/api/health?refresh=true");
    renderHealth(health);
    applyAdapterAvailability(health);
    toast("Health refreshed.");
  } catch (error) {
    toast(error.message);
  } finally {
    $("#health-refresh").disabled = false;
  }
});

document.addEventListener("keydown", event => {
  const target = event.target;
  const editing = (
    target instanceof HTMLElement &&
    (target.matches("input, textarea, select") || target.isContentEditable)
  );

  if (
    event.key === "/" &&
    !editing &&
    !event.ctrlKey &&
    !event.metaKey &&
    !event.altKey &&
    !document.querySelector("dialog[open]")
  ) {
    event.preventDefault();
    $("#search").focus();
  }
});

document.addEventListener("visibilitychange", () => {
  if (!document.hidden) refresh();
});

updateNavigation();
renderComparison();
api("/api/benchmarks").then(renderCatalog).catch(() => {
  $("#suite-grid").innerHTML =
    '<p class="subtle">Could not load the fixture suite from the API.</p>';
});
refresh();
