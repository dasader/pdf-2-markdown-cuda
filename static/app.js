const $ = (s) => document.querySelector(s);
const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;

const dropEl = $("#drop");
const fileInput = $("#file");
const optImg = $("#opt-img");
const optCsv = $("#opt-csv");
const queueEl = $("#queue");
const beatEl = $("#beat");
const downallEl = $("#downall");
const adminBtn = $("#admin");
const modalEl = $("#modal");
const modalFn = $("#modal-fn");
const mdEl = $("#md");

const state = new Map(); // id -> job
let busy = false;
let adminKey = localStorage.getItem("pdf2md-admin-key") || null;
let sse = null;
let pollTimer = null;

// ---- helpers ----

function apiFetch(url, opts = {}) {
  const headers = Object.assign({}, opts.headers || {});
  if (adminKey) headers["X-Admin-Key"] = adminKey;
  return fetch(url, Object.assign({}, opts, { headers }));
}

// blob download that carries X-Admin-Key (plain <a>/location.href navigation can't send headers)
async function download(url, filename) {
  const res = await apiFetch(url);
  if (!res.ok) {
    alert("다운로드에 실패했습니다.");
    return;
  }
  const blob = await res.blob();
  const objurl = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = objurl;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(objurl), 1000);
}

function stateText(j) {
  if (j.status === "done") return "완료";
  if (j.status === "running") return (j.progress | 0) + "%";
  if (j.status === "failed") return "실패";
  const ahead = j.ahead || 0;
  return ahead === 0 ? "곧 시작" : `앞에 ${ahead}개 대기`;
}

// ---- render (keyed reconciliation) ----
// Reuse existing job elements instead of rebuilding innerHTML each SSE tick.
// Rebuilding replayed the entrance animation on every tick (flicker), reset the
// progress-bar transition, and wiped the "복사됨" flash mid-copy. Here each job
// keeps its DOM node; only changed fields are patched, and done-meta buttons
// (with their click handlers) are created once and never rebuilt.

const cards = new Map(); // id -> element

function buildCard(j) {
  const el = document.createElement("div");
  el.className = "job " + j.status;
  el.dataset.id = j.id;
  el.innerHTML =
    '<span class="node"></span>' +
    '<div class="job-row"><span class="name"></span><span class="state"></span></div>' +
    '<div class="bar"><i></i></div>';
  el.querySelector(".name").textContent = j.filename || "";
  patchCard(el, j);
  return el;
}

// Write only what actually changed. render() patches EVERY card each tick, and
// admin mode polls a full snapshot of ~200 jobs — unconditional writes meant
// ~600 DOM writes every tick. className forces a style recalc and textContent
// replaces the text node even when the string is identical, so the whole queue
// repainted on every poll: the flicker. Only the running job changes per tick.
function patchCard(el, j) {
  const cls = "job " + j.status;
  if (el.className !== cls) el.className = cls;

  const stateEl = el.querySelector(".state");
  const txt = stateText(j);
  if (stateEl.textContent !== txt) stateEl.textContent = txt;

  const w = (j.status === "running" ? (j.progress | 0) : j.status === "done" ? 100 : 0) + "%";
  const bar = el.querySelector(".bar i");
  if (bar.style.width !== w) bar.style.width = w;

  let err = el.querySelector(".err");
  if (j.status === "failed" && j.error) {
    if (!err) {
      err = document.createElement("div");
      err.className = "err";
      el.appendChild(err);
    }
    if (err.textContent !== j.error) err.textContent = j.error;
  } else if (err) {
    err.remove();
  }

  // done-meta: create once on the transition to done, never rebuild (so an
  // in-progress "복사됨" flash on the copy button survives later ticks).
  if (j.status === "done" && !el.querySelector(".done-meta")) {
    const meta = document.createElement("div");
    meta.className = "done-meta";
    meta.innerHTML =
      '<span class="chip"></span>' +
      '<button class="act" type="button">미리보기</button>' +
      '<button class="act ghost" type="button">마크다운 복사</button>' +
      '<button class="act ghost" type="button">MD 내려받기</button>' +
      '<button class="act ghost" type="button">ZIP 내려받기</button>';
    meta.querySelector(".chip").textContent = `표 ${j.n_tables || 0} · 이미지 ${j.n_images || 0}`;
    const btns = meta.querySelectorAll("button");
    const stem = (j.filename ? j.filename.replace(/\.[^./]+$/, "") : "") || "result";
    btns[0].onclick = () => openPreview(j.id);
    btns[1].onclick = () => copyMd(j.id, btns[1]);
    // ponytail: MD만 받기는 preview 응답(마크다운 원문)을 그대로 파일로 저장한다.
    // 전용 엔드포인트 불필요 — download()가 blob + a.download로 파일명을 붙인다.
    btns[2].onclick = () => download(`/api/jobs/${j.id}/preview`, `${stem}.md`);
    btns[3].onclick = () => download(`/api/jobs/${j.id}/download`, `${stem}.zip`);
    el.appendChild(meta);
  }
}

function render() {
  const jobs = [...state.values()].sort((a, b) => b.created_at - a.created_at);

  const myRunning = jobs.some((j) => j.status === "running");
  const showBeat = busy && !myRunning;
  beatEl.classList.toggle("show", showBeat);
  beatEl.classList.toggle("hide", !showBeat);

  const myDone = jobs.filter((j) => j.status === "done").length;
  downallEl.classList.toggle("hide", myDone < 2);

  // Move a card only when it's actually out of place. Admin mode polls every 2s
  // and re-renders unconditionally; appendChild-ing all ~200 nodes each tick
  // re-laid-out the whole queue even when nothing reordered — the flicker on long
  // lists. Walking a cursor means zero DOM moves in the steady state (no reorder).
  const seen = new Set();
  let created = 0;
  let cursor = queueEl.firstChild;
  jobs.forEach((j) => {
    seen.add(j.id);
    let el = cards.get(j.id);
    if (!el) {
      el = buildCard(j);
      if (!reduce) el.style.animationDelay = created++ * 45 + "ms";
      cards.set(j.id, el);
    } else {
      patchCard(el, j);
    }
    if (el === cursor) {
      cursor = cursor.nextSibling; // already in sorted position; leave it untouched
    } else {
      queueEl.insertBefore(el, cursor); // insert new / move out-of-order card into place
    }
  });

  cards.forEach((el, id) => {
    if (!seen.has(id)) {
      el.remove();
      cards.delete(id);
    }
  });
}

// ---- job state sync ----

function applyFull(data) {
  state.clear();
  (data.jobs || []).forEach((j) => state.set(j.id, j));
  busy = !!data.busy;
  render();
}

function applyDelta(data) {
  const settled = [];
  (data.jobs || []).forEach((j) => {
    const prev = state.get(j.id);
    if (prev && prev.status !== "done" && j.status === "done") settled.push(j.id);
    state.set(j.id, j);
  });
  busy = !!data.busy;
  render();
  settled.forEach((id) => {
    const node = queueEl.querySelector(`.job[data-id="${id}"] .node`);
    if (node) {
      node.classList.add("settle");
      setTimeout(() => node.classList.remove("settle"), 320);
    }
  });
}

async function refresh() {
  const res = await apiFetch("/api/jobs");
  applyFull(await res.json());
}

// ---- live updates: SSE normally, poll while admin (EventSource can't carry X-Admin-Key) ----

function connectSSE() {
  // adminKey: onerror only checks it when QUEUEING the reconnect. The server ends
  // each stream after 5 min to force a reconnect, so a timer can already be armed
  // when the user types the admin key — it then fired into admin mode and ran an
  // SSE stream (0.5s ticks, and session-scoped since EventSource can't send the
  // header) alongside the 2s poll. Re-check here, at fire time.
  if (sse || adminKey) return;
  sse = new EventSource("/api/events");
  sse.onmessage = (e) => applyDelta(JSON.parse(e.data));
  sse.onerror = () => {
    if (sse) { sse.close(); sse = null; }
    if (!adminKey) setTimeout(connectSSE, 2000);
  };
}
function stopSSE() {
  if (sse) { sse.close(); sse = null; }
}
function startPolling() {
  if (pollTimer) return;
  pollTimer = setInterval(refresh, 2000);
}
function stopPolling() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
}

// ---- upload ----

$("#pick").onclick = () => fileInput.click();
fileInput.onchange = () => {
  upload([...fileInput.files]);
  fileInput.value = "";
};

["dragover", "dragenter"].forEach((e) =>
  dropEl.addEventListener(e, (ev) => {
    ev.preventDefault();
    dropEl.classList.add("over");
  })
);
["dragleave", "drop"].forEach((e) =>
  dropEl.addEventListener(e, (ev) => {
    ev.preventDefault();
    dropEl.classList.remove("over");
  })
);
dropEl.addEventListener("drop", (ev) => upload([...ev.dataTransfer.files]));

async function upload(files) {
  files = files.filter((f) => f.name.toLowerCase().endsWith(".pdf"));
  if (!files.length) return;
  const fd = new FormData();
  files.forEach((f) => fd.append("files", f));
  fd.append("include_images", optImg.checked ? "true" : "false");
  fd.append("include_tables_csv", optCsv.checked ? "true" : "false");
  await apiFetch("/api/jobs", { method: "POST", body: fd });
  await refresh();
}

// ---- preview / copy ----

async function openPreview(id) {
  const j = state.get(id);
  modalFn.textContent = j ? j.filename : "";
  mdEl.textContent = "불러오는 중…";
  modalEl.classList.add("open");
  try {
    const md = await (await apiFetch(`/api/jobs/${id}/preview`)).text();
    mdEl.innerHTML = marked.parse(md);
    mdEl.querySelectorAll("table").forEach((t) => {
      if (!t.parentElement.classList.contains("tw")) {
        const wrap = document.createElement("div");
        wrap.className = "tw";
        t.parentNode.insertBefore(wrap, t);
        wrap.appendChild(t);
      }
    });
  } catch (e) {
    mdEl.textContent = "미리보기를 불러오지 못했습니다.";
  }
}

// ponytail: 접속은 localhost 또는 SSH 터널(=secure context) 전제. http://<서버IP>로
// 직접 열면 navigator.clipboard가 없어 복사가 실패하고 copyMd가 "복사 실패"를 띄운다.
async function copyText(text) {
  await navigator.clipboard.writeText(text);
}

async function copyMd(id, btn) {
  try {
    const md = await (await apiFetch(`/api/jobs/${id}/preview`)).text();
    await copyText(md);
    const orig = btn.textContent;
    btn.textContent = "복사됨";
    btn.classList.add("copied");
    setTimeout(() => {
      btn.textContent = orig;
      btn.classList.remove("copied");
    }, 1400);
  } catch (e) {
    /* silent — preview fetch failed */
  }
}

// ---- modal chrome ----

function closeModal() {
  modalEl.classList.remove("open");
}
$("#close").onclick = closeModal;
modalEl.onclick = (e) => {
  if (e.target === modalEl) closeModal();
};
addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeModal();
});

// ---- download all ----

downallEl.onclick = () => download("/api/download-all", "pdf2md-변환결과.zip");

// ---- admin toggle ----

adminBtn.onclick = () => {
  if (adminKey) {
    adminKey = null;
    localStorage.removeItem("pdf2md-admin-key");
    adminBtn.textContent = "전체 보기";
    stopPolling();
    connectSSE();
  } else {
    const key = prompt("관리자 키를 입력하세요");
    if (!key) return;
    adminKey = key;
    localStorage.setItem("pdf2md-admin-key", key);
    adminBtn.textContent = "내 작업만";
    stopSSE();
    startPolling();
  }
  refresh();
};

// ---- boot ----

if (adminKey) {
  adminBtn.textContent = "내 작업만";
  startPolling();
} else {
  connectSSE();
}
refresh();
