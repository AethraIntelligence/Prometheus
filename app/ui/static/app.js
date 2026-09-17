/* The page. No framework, no build step: the whole interface is one request,
   one event stream and a handful of DOM updates, and a toolchain to produce
   that would be a second thing to install on a machine whose selling point is
   that it needs nothing installed.

   Since Phase 7 what the page submits is an objective, not a task: the user
   states an outcome and Prometheus decides what that takes and who does it. The trace
   below it is the manager's own progress interleaved with that of every task it
   started, which the server merges - the page does not have to know that the
   two come from different places.

   Two rules keep it honest.

   The trace is appended from the stream, never re-rendered from a poll: what
   the page shows while a task runs is what the runtime said as it happened.
   Opening a past task, by contrast, draws from the stored tool calls - the
   stream buffer is short-lived and is not the record.

   Nothing is approved by the page. The buttons send a decision; the run stays
   parked until the server answers, and a closed page means an unanswered
   question, which the server treats as a no. */

const el = (id) => document.getElementById(id);

const state = {
  selected: null,   // the objective whose trace is on screen
  stream: null,     // the EventSource for it
};

// --- Talking to the server --------------------------------------------------

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || `${response.status} ${response.statusText}`);
  }
  return response.status === 204 ? null : response.json();
}

// --- Starting work ----------------------------------------------------------

async function loadEmployees() {
  // Named, not chosen: who exists is worth seeing, but picking one is the
  // manager's job now, and a dropdown would invite the user to do it instead.
  const { employees } = await api("/api/employees");
  el("workforce").textContent = employees.length
    ? `Available: ${employees.map((e) => e.name).join(", ")}.`
    : "Nobody is declared yet - add one under employees/.";
  el("run").disabled = employees.length === 0;
}

el("new-objective").addEventListener("submit", async (event) => {
  event.preventDefault();
  const error = el("form-error");
  error.hidden = true;
  el("run").disabled = true;
  try {
    const objective = await api("/api/objectives", {
      method: "POST",
      body: JSON.stringify({ request: el("request").value }),
    });
    el("request").value = "";
    await refreshHistory();
    select(objective.id);
  } catch (failure) {
    error.textContent = failure.message;
    error.hidden = false;
  } finally {
    el("run").disabled = false;
  }
});

// --- History ----------------------------------------------------------------

async function refreshHistory() {
  const { objectives } = await api("/api/objectives");
  const list = el("history");
  list.innerHTML = "";
  for (const item of objectives) {
    const node = document.createElement("li");
    node.className = item.id === state.selected ? "selected" : "";
    node.innerHTML = `<span class="goal"></span>
      <span class="sub"><span class="status status-${item.status}"></span>
      &middot; $${item.cost_usd.toFixed(4)}</span>`;
    node.querySelector(".goal").textContent = item.text;
    node.querySelector(".status").textContent = item.thinking ? "WORKING" : item.status;
    node.addEventListener("click", () => select(item.id));
    list.append(node);
  }
}

// --- One run ----------------------------------------------------------------

async function select(objectiveId) {
  state.selected = objectiveId;
  if (state.stream) state.stream.close();
  el("trace").innerHTML = "";

  const objective = await api(`/api/objectives/${objectiveId}`);
  drawHeader(objective);
  drawPlans(objective);
  await refreshHistory();

  // The server replays what it has buffered for this objective and for every
  // task in its plan, then follows both. A finished objective's stream ends on
  // its own after the replay.
  state.stream = new EventSource(`/api/events?objective=${objectiveId}`);
  state.stream.onmessage = (message) => onProgress(JSON.parse(message.data));
}

function drawHeader(objective) {
  el("run-goal").textContent = objective.text;
  const status = objective.thinking ? "WORKING" : objective.status;
  el("run-meta").innerHTML = `<span class="status status-${objective.status}"></span>
    &middot; $${objective.cost_usd.toFixed(6)}`;
  el("run-meta").querySelector(".status").textContent = status;
  el("cancel").hidden = !objective.thinking;

  const result = el("run-result");
  const answer = objective.result;
  if (answer && answer.summary) {
    result.hidden = false;
    result.className = objective.status === "DONE" ? "" : "failed";
    result.textContent = answer.summary;
    if (answer.missing && answer.missing.length) {
      const missing = document.createElement("ul");
      missing.className = "criteria";
      for (const item of answer.missing) {
        const line = document.createElement("li");
        line.className = "unmet";
        line.textContent = item;
        missing.append(line);
      }
      result.append(missing);
    }
  } else {
    result.hidden = true;
  }
}

function drawPlans(objective) {
  const holder = el("run-plan");
  holder.innerHTML = "";
  holder.hidden = !objective.plans.length;
  // Newest revision first, and the superseded ones are kept on screen: what
  // Prometheus tried the first time is why there was a second time.
  for (const plan of objective.plans) {
    const node = document.createElement("div");
    node.className = `plan${plan.status === "SUPERSEDED" ? " superseded" : ""}`;
    node.innerHTML = `<p class="why"></p><ol></ol>`;
    node.querySelector(".why").textContent =
      `Plan ${plan.revision} (${plan.status})` + (plan.rationale ? ` - ${plan.rationale}` : "");
    const list = node.querySelector("ol");
    for (const task of plan.tasks) {
      const line = document.createElement("li");
      line.innerHTML = `<span class="goal"></span> <span class="task-status"></span>`;
      line.querySelector(".goal").textContent = task.goal;
      line.querySelector(".task-status").textContent = task.status;
      list.append(line);
    }
    holder.append(node);
  }
}

el("cancel").addEventListener("click", async () => {
  if (!state.selected) return;
  el("cancel").disabled = true;
  try {
    await api(`/api/objectives/${state.selected}/cancel`, { method: "POST" });
  } finally {
    el("cancel").disabled = false;
  }
});

function line({ step, kind, message, failed, interfaceLevel }) {
  const item = document.createElement("li");
  item.className = `kind-${kind}${failed ? " failed" : ""}`;
  item.innerHTML = `<span class="step"></span><span class="kind"></span>
    <span class="message"></span>`;
  item.querySelector(".step").textContent = step ? `#${step}` : "";
  item.querySelector(".kind").textContent = interfaceLevel || kind.toLowerCase();
  item.querySelector(".message").textContent = message;
  el("trace").append(item);
  item.scrollIntoView({ block: "nearest" });
}

// --- The stream -------------------------------------------------------------

function onProgress(event) {
  line({
    step: event.step,
    kind: event.kind,
    interfaceLevel: event.payload.interface,
    message: event.message,
    failed: event.payload.succeeded === false || event.payload.status === "FAILED",
  });
  if (event.kind === "RESULT" || event.kind === "APPROVAL") {
    refreshTask();
    refreshApprovals();
    refreshSpend();
  }
  if (event.kind === "PLAN") refreshTask();
  if (event.kind === "RESULT" && event.objective_id && state.stream) {
    // The server ends the stream when the task ends. Closing it here too is
    // what stops EventSource from treating that as a dropped connection and
    // reconnecting to a run that has nothing left to say.
    state.stream.close();
    state.stream = null;
  }
}

async function refreshTask() {
  if (!state.selected) return;
  const objective = await api(`/api/objectives/${state.selected}`);
  drawHeader(objective);
  drawPlans(objective);
  await refreshHistory();
}

// --- Approvals --------------------------------------------------------------

async function refreshApprovals() {
  const { approvals } = await api("/api/approvals");
  el("approvals").hidden = approvals.length === 0;
  const list = el("approval-list");
  list.innerHTML = "";
  for (const item of approvals) {
    const node = document.createElement("li");
    node.innerHTML = `<div class="action"></div><p class="why"></p><p class="scope"></p>
      <label>Permission <select class="grant">
        <option value="ONCE">Only this action</option>
        <option value="TASK">This exact action for this task</option>
        <option value="PERSISTENT">Remember this exact rule</option>
      </select></label>
      <label class="expiry" hidden>Expires <select>
        <option value="3600">In 1 hour</option>
        <option value="86400" selected>In 24 hours</option>
        <option value="2592000">In 30 days</option>
        <option value="">Never</option>
      </select></label>
      <button type="button" class="approve">Approve</button>
      <button type="button" class="secondary reject">Reject</button>`;
    node.querySelector(".action").textContent = item.action;
    node.querySelector(".why").textContent =
      `${item.risk}${item.reason ? ` - ${item.reason}` : ""}`;
    node.querySelector(".scope").textContent = item.scope
      ? `Exact scope: ${item.scope.action} on ${item.scope.resource}`
      : "";
    const grant = node.querySelector(".grant");
    const expiry = node.querySelector(".expiry");
    grant.addEventListener("change", () => { expiry.hidden = grant.value !== "PERSISTENT"; });
    node.querySelector(".approve").addEventListener("click", () => {
      const duration = grant.value === "PERSISTENT" ? expiry.querySelector("select").value : "";
      decide(item.id, true, grant.value, duration ? Number(duration) : null);
    });
    node.querySelector(".reject").addEventListener("click", () => decide(item.id, false));
    list.append(node);
  }
}

async function decide(approvalId, approved, grant = "ONCE", durationSeconds = null) {
  await api(`/api/approvals/${approvalId}`, {
    method: "POST",
    body: JSON.stringify({
      approved,
      comment: "",
      grant,
      duration_seconds: durationSeconds,
    }),
  });
  await refreshApprovals();
}

// --- Cost -------------------------------------------------------------------

async function refreshSpend() {
  const spend = await api("/api/spend");
  el("spend").textContent =
    `${spend.calls} calls - $${spend.cost_usd.toFixed(6)}`;
}

// --- Start ------------------------------------------------------------------

async function start() {
  await loadEmployees();
  await refreshHistory();
  await refreshApprovals();
  await refreshSpend();
  // An approval raised by a task nobody has open still has to appear, so the
  // page watches everything as well as the run it is showing.
  const all = new EventSource("/api/events");
  all.onmessage = (message) => {
    const event = JSON.parse(message.data);
    if (event.kind === "APPROVAL") refreshApprovals();
    if (event.kind === "RESULT" && event.objective_id !== state.selected) refreshHistory();
  };
}

start().catch((failure) => {
  const error = el("form-error");
  error.textContent = failure.message;
  error.hidden = false;
});
