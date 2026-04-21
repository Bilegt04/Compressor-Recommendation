// ===========================================================================
// Image Compression Recommender — frontend
//
// Upload architecture:
// - A <label id="dropzone"> wraps a visually-hidden <input type="file">.
//   Clicking anywhere on the label natively opens the file picker
//   (via the for= attribute). This gives us one control, not two.
// - Dragged files are stored in a module-level variable `currentFile`
//   instead of being round-tripped through `input.files`. DataTransfer's
//   cross-browser reliability is poor, so we submit via FormData.append
//   using whatever is in currentFile. Source of truth = JS state.
// ===========================================================================

const STRINGS = {
  dropPrompt: "Drag & drop image here or click to upload",
  dropActive: "Release to add the image",
  analyzing: "Analyzing — this usually takes 2 to 10 seconds…",
  done: "Done.",
  errors: {
    noFile: "Please select an image file first.",
    tooLarge: (mb) => `That file is too large. The maximum is ${mb} MB.`,
    wrongType: "Unsupported file type. Please choose a JPEG, PNG, or WebP image.",
    network: "Could not reach the server. Please try again.",
    server: (code) => `Something went wrong (${code}).`,
    tooManyFiles: "Please drop only one file at a time.",
  },
};

const ACCEPTED_MIME = new Set(["image/jpeg", "image/png", "image/webp"]);
const ACCEPTED_EXT = /\.(jpe?g|png|webp)$/i;

// --- DOM refs ---
const form = document.getElementById("upload-form");
const fileInput = document.getElementById("file-input");
const dropzone = document.getElementById("dropzone");
const dropzoneEmpty = document.getElementById("dropzone-empty");
const dropzoneSelected = document.getElementById("dropzone-selected");
const previewThumb = document.getElementById("preview-thumb");
const selectedFilename = document.getElementById("selected-filename");
const selectedMeta = document.getElementById("selected-meta");
const removeBtn = document.getElementById("remove-btn");
const submitBtn = document.getElementById("submit-btn");
const statusEl = document.getElementById("status");

const emptyState = document.getElementById("empty-state");
const recPanel = document.getElementById("recommendation-panel");
const recLabel = document.getElementById("rec-label");
const recHeadline = document.getElementById("rec-headline");
const recWhy = document.getElementById("rec-why");
const recComparison = document.getElementById("rec-comparison");
const downloadBtn = document.getElementById("download-btn");

const previewPanel = document.getElementById("preview-panel");
const previewImg = document.getElementById("preview-img");
const imageMeta = document.getElementById("image-meta");

const variantsPanel = document.getElementById("variants-panel");
const variantsGrid = document.getElementById("variants-grid");

const devPanel = document.getElementById("dev-panel");
const devExports = document.getElementById("dev-exports");

// Upload state — the ONE source of truth for the selected file.
let currentFile = null;
let currentThumbUrl = null;
let configFlags = { oam_features_enabled: false, max_upload_bytes: 0 };

// ===========================================================================
// Bootstrap
// ===========================================================================
(async function init() {
  try {
    const r = await fetch("/config");
    if (r.ok) configFlags = await r.json();
  } catch (_) { /* defaults OK */ }

  if (configFlags.oam_features_enabled) {
    devPanel.hidden = false;
    devExports.innerHTML = `
      <a href="/exports/raw_results.csv" target="_blank" rel="noopener">raw_results.csv</a>
      <a href="/exports/attribute_dictionary.csv" target="_blank" rel="noopener">attribute_dictionary.csv</a>
      <a href="/exports/oam.csv?oam_variant=minimal" target="_blank" rel="noopener">oam.csv (minimal)</a>
      <a href="/exports/oam.csv?oam_variant=extended" target="_blank" rel="noopener">oam.csv (extended)</a>
      <a href="/exports/analysis.csv" target="_blank" rel="noopener">analysis.csv</a>
    `;
  }

  initCocoPanel();
  initCocoComparePanel();
})();

// ===========================================================================
// Helpers
// ===========================================================================
function setStatus(text, kind) {
  statusEl.textContent = text || "";
  statusEl.className = "status-line" + (kind ? " " + kind : "");
}

function formatKb(v) {
  if (typeof v !== "number") return "—";
  return v < 10 ? v.toFixed(2) + " KB" : v.toFixed(1) + " KB";
}
function formatPercent(v) {
  if (typeof v !== "number") return "—";
  return v.toFixed(1) + "%";
}
function formatBytes(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(2)} MB`;
}

function isAcceptedFile(file) {
  if (!file) return false;
  if (file.type && ACCEPTED_MIME.has(file.type)) return true;
  return ACCEPTED_EXT.test(file.name || "");
}

function validateFileSize(file) {
  if (!configFlags.max_upload_bytes) return null;
  if (file.size > configFlags.max_upload_bytes) {
    const mb = Math.floor(configFlags.max_upload_bytes / (1024 * 1024));
    return STRINGS.errors.tooLarge(mb);
  }
  return null;
}

// ===========================================================================
// File selection — single code path for drop, click, and picker change
// ===========================================================================
function acceptFile(file) {
  if (!isAcceptedFile(file)) {
    setStatus(STRINGS.errors.wrongType, "error");
    resetFile();
    return false;
  }
  const sizeErr = validateFileSize(file);
  if (sizeErr) {
    setStatus(sizeErr, "error");
    resetFile();
    return false;
  }

  // Revoke any previous thumbnail to free memory.
  if (currentThumbUrl) URL.revokeObjectURL(currentThumbUrl);

  currentFile = file;
  currentThumbUrl = URL.createObjectURL(file);

  previewThumb.src = currentThumbUrl;
  previewThumb.alt = `Preview of ${file.name}`;
  selectedFilename.textContent = file.name;
  selectedMeta.textContent = formatBytes(file.size);

  dropzoneEmpty.hidden = true;
  dropzoneSelected.hidden = false;
  dropzone.classList.add("has-file");
  submitBtn.disabled = false;
  setStatus("", "");
  return true;
}

function resetFile() {
  if (currentThumbUrl) {
    URL.revokeObjectURL(currentThumbUrl);
    currentThumbUrl = null;
  }
  currentFile = null;
  fileInput.value = "";        // clear native picker state too
  previewThumb.src = "";
  selectedFilename.textContent = "";
  selectedMeta.textContent = "";
  dropzoneEmpty.hidden = false;
  dropzoneSelected.hidden = true;
  dropzone.classList.remove("has-file");
  submitBtn.disabled = true;
}

// Picker path — user clicked the dropzone, browser opened the picker.
fileInput.addEventListener("change", () => {
  const file = fileInput.files && fileInput.files[0];
  if (file) acceptFile(file);
});

// Remove button — explicit reset.
removeBtn.addEventListener("click", (e) => {
  // Stop the label from re-opening the picker on the same click.
  e.preventDefault();
  e.stopPropagation();
  resetFile();
});

// ===========================================================================
// Drag & drop
// ===========================================================================

// Stop the browser's default "navigate to file" when a file is dropped
// anywhere OUTSIDE the dropzone. Without this, a missed drop loads the
// image full-screen and wipes the page. Must be non-passive to preventDefault.
window.addEventListener("dragover", (e) => e.preventDefault());
window.addEventListener("drop", (e) => e.preventDefault());

function setDragActive(active) {
  dropzone.classList.toggle("is-dragover", active);
  const primary = dropzoneEmpty.querySelector(".dropzone-primary");
  if (primary) {
    primary.textContent = active ? STRINGS.dropActive : STRINGS.dropPrompt;
  }
}

// Drag enters OR moves over the dropzone — keep highlight on, prevent
// default so `drop` fires.
["dragenter", "dragover"].forEach(evt => {
  dropzone.addEventListener(evt, (e) => {
    e.preventDefault();
    e.stopPropagation();
    setDragActive(true);
  });
});

// Drag leaves. Note: dragleave fires when crossing ANY child element, so
// we only clear the highlight if the pointer actually leaves the dropzone
// (relatedTarget is outside, or null).
dropzone.addEventListener("dragleave", (e) => {
  if (!dropzone.contains(e.relatedTarget)) setDragActive(false);
});
dropzone.addEventListener("dragend", () => setDragActive(false));

dropzone.addEventListener("drop", (e) => {
  e.preventDefault();
  e.stopPropagation();
  setDragActive(false);

  const files = e.dataTransfer && e.dataTransfer.files;
  if (!files || files.length === 0) return;
  if (files.length > 1) {
    setStatus(STRINGS.errors.tooManyFiles, "error");
    return;
  }
  acceptFile(files[0]);
});

// ===========================================================================
// Rendering results
// ===========================================================================
function renderRecommendation(data) {
  const rec = data.recommendation;
  recLabel.textContent = rec.label;
  recHeadline.textContent = rec.headline;
  recWhy.textContent = rec.why;

  recComparison.innerHTML = "";
  (rec.comparison || []).forEach(line => {
    const li = document.createElement("li");
    li.textContent = line;
    recComparison.appendChild(li);
  });

  recPanel.className =
    "panel recommendation quality-" + (rec.quality_indicator || "acceptable");

  downloadBtn.href =
    `/images/${data.image_id}/variant/${data.recommended_variant_key}`;
  downloadBtn.setAttribute(
    "download",
    `${data.image_id}_${data.recommended_variant_key}`
  );

  recPanel.hidden = false;
}

function renderPreview(data) {
  previewImg.src = `/images/${data.image_id}/preview`;
  const dims = (data.width_px && data.height_px)
    ? `${data.width_px} × ${data.height_px} px — `
    : "";
  imageMeta.textContent =
    `${dims}original size ${formatKb(data.original_size_kb)}`;
  previewPanel.hidden = false;
}

function renderVariants(data) {
  variantsGrid.innerHTML = "";
  const sorted = [...data.variants].sort((a, b) => {
    if (a.is_recommended !== b.is_recommended) return a.is_recommended ? -1 : 1;
    if (a.format_name !== b.format_name) return a.format_name.localeCompare(b.format_name);
    return b.encoder_quality_param - a.encoder_quality_param;
  });

  for (const v of sorted) {
    const card = document.createElement("div");
    card.className = "variant-card" +
      (v.is_recommended ? " is-recommended" : "") +
      (v.is_efficient && !v.is_recommended ? " is-efficient" : "");

    const badges = [];
    if (v.is_recommended) {
      badges.push('<span class="badge recommended">Recommended</span>');
    } else if (v.is_efficient) {
      badges.push('<span class="badge efficient">Efficient option</span>');
    }

    card.innerHTML = `
      <div class="format-title">${v.format_name}, quality ${v.encoder_quality_param}${badges.join("")}</div>
      <div class="stat-row"><span class="stat-label">File size</span><span class="stat-value">${formatKb(v.compressed_size_kb)}</span></div>
      <div class="stat-row"><span class="stat-label">Saved</span><span class="stat-value">${formatPercent(v.percent_saved)}</span></div>
      <div class="stat-row"><span class="stat-label" title="Peak signal-to-noise ratio — a technical quality measure. Higher is better.">PSNR</span><span class="stat-value">${typeof v.psnr === "number" ? v.psnr.toFixed(2) + " dB" : "—"}</span></div>
      <div class="stat-row"><span class="stat-label" title="Structural similarity to the original. 1.0 means identical.">SSIM</span><span class="stat-value">${typeof v.ssim === "number" ? v.ssim.toFixed(4) : "—"}</span></div>
    `;
    variantsGrid.appendChild(card);
  }
  variantsPanel.hidden = false;
}

function renderAll(data) {
  emptyState.hidden = true;
  renderRecommendation(data);
  renderPreview(data);
  renderVariants(data);
}

// ===========================================================================
// Submit
// ===========================================================================
form.addEventListener("submit", async (e) => {
  e.preventDefault();

  if (!currentFile) {
    setStatus(STRINGS.errors.noFile, "error");
    return;
  }

  submitBtn.disabled = true;
  setStatus(STRINGS.analyzing, "loading");

  const fd = new FormData();
  fd.append("file", currentFile, currentFile.name);

  try {
    const resp = await fetch("/upload", { method: "POST", body: fd });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      const detail = err && err.detail
        ? err.detail
        : STRINGS.errors.server(resp.status);
      setStatus(detail, "error");
      return;
    }
    const data = await resp.json();
    setStatus(STRINGS.done, "");
    renderAll(data);
  } catch (_) {
    setStatus(STRINGS.errors.network, "error");
  } finally {
    submitBtn.disabled = !currentFile;
  }
});

// ===========================================================================
// COCO Y0 build panel
// ===========================================================================
function initCocoPanel() {
  const variantSel = document.getElementById("coco-variant");
  const stepInput = document.getElementById("coco-step-count");
  const buildBtn = document.getElementById("coco-build-btn");
  const statusEl2 = document.getElementById("coco-status");
  const outputBox = document.getElementById("coco-output");
  const summaryEl = document.getElementById("coco-summary");
  const matrixHead = document.getElementById("coco-matrix-head");
  const matrixBody = document.getElementById("coco-matrix-body");
  const matrixRaw = document.getElementById("coco-matrix-raw");
  const objectsEl = document.getElementById("coco-objects");
  const attributesEl = document.getElementById("coco-attributes");
  const cocoDownloadBtn = document.getElementById("coco-download-btn");

  function setCocoStatus(text, kind) {
    statusEl2.textContent = text || "";
    statusEl2.className = "status-line" + (kind ? " " + kind : "");
  }

  function updateDownloadHref() {
    const v = encodeURIComponent(variantSel.value);
    const s = encodeURIComponent(stepInput.value || "0");
    cocoDownloadBtn.href = `/coco/download?oam_variant=${v}&step_count=${s}`;
  }

  // Render the ranked matrix as a real HTML table. One cell per rank value.
  // Column headers are suffixed with "_rank" to make it unambiguous that
  // these are ranks (1 = best), not raw metric values.
  function renderRankMatrixTable(data) {
    matrixHead.innerHTML = "";
    const thOid = document.createElement("th");
    thOid.textContent = "object_id";
    matrixHead.appendChild(thOid);
    for (const a of data.attributes) {
      const th = document.createElement("th");
      th.textContent = a + "_rank";
      matrixHead.appendChild(th);
    }

    matrixBody.innerHTML = "";
    for (let i = 0; i < data.object_ids.length; i++) {
      const tr = document.createElement("tr");
      const tdOid = document.createElement("td");
      tdOid.textContent = data.object_ids[i];
      tdOid.className = "cell-oid";
      tr.appendChild(tdOid);
      for (const v of data.ranked_matrix[i]) {
        const td = document.createElement("td");
        td.textContent = String(v);
        td.className = "cell-rank";
        tr.appendChild(td);
      }
      matrixBody.appendChild(tr);
    }

    // Keep the raw tab-separated form verbatim in a hidden element for the
    // Copy button. This is what the external solver expects as input.
    matrixRaw.textContent = data.matrix_text;
  }

  async function buildPreview() {
    setCocoStatus("Building ranked input…", "loading");
    outputBox.hidden = true;
    const v = encodeURIComponent(variantSel.value);
    const s = encodeURIComponent(stepInput.value || "0");
    try {
      const r = await fetch(`/coco/preview?oam_variant=${v}&step_count=${s}`);
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        setCocoStatus(err.detail || `Error (${r.status}).`, "error");
        return;
      }
      const data = await r.json();
      summaryEl.textContent =
        `${data.n_objects} objects × ${data.n_attributes} attributes — ` +
        `attributes: ${data.attributes.join(", ")} ` +
        `(directions: ${data.directions.join(", ")}; 1 = best). ` +
        `Step count: ${data.step_count === 0 ? "full ranking" : data.step_count}.`;
      renderRankMatrixTable(data);
      objectsEl.textContent = data.object_list_text;
      attributesEl.textContent = data.attribute_list_text;
      updateDownloadHref();
      outputBox.hidden = false;
      setCocoStatus("Ready.", "");
    } catch (_) {
      setCocoStatus(STRINGS.errors.network, "error");
    }
  }

  buildBtn.addEventListener("click", buildPreview);
  variantSel.addEventListener("change", () => { if (!outputBox.hidden) buildPreview(); });
  stepInput.addEventListener("change", () => { if (!outputBox.hidden) buildPreview(); });

  document.querySelectorAll(".copy-btn").forEach(btn => {
    btn.addEventListener("click", async () => {
      // Either data-target (points at a visible <pre>) or data-source
      // (logical name — currently only "matrix" → the hidden raw matrix).
      let text = "";
      const targetId = btn.getAttribute("data-target");
      const source = btn.getAttribute("data-source");
      if (targetId) {
        const target = document.getElementById(targetId);
        if (!target) return;
        text = target.textContent || "";
      } else if (source === "matrix") {
        const raw = document.getElementById("coco-matrix-raw");
        text = (raw && raw.textContent) || "";
      }
      if (!text) return;

      try {
        await navigator.clipboard.writeText(text);
        const original = btn.textContent;
        btn.textContent = "Copied ✓";
        btn.classList.add("copied");
        setTimeout(() => {
          btn.textContent = original;
          btn.classList.remove("copied");
        }, 1500);
      } catch (_) {
        // Fallback: copy via a temporary textarea. Works on insecure origins
        // and on browsers that block clipboard writes from non-user gestures.
        const ta = document.createElement("textarea");
        ta.value = text;
        ta.style.position = "fixed";
        ta.style.opacity = "0";
        document.body.appendChild(ta);
        ta.select();
        try { document.execCommand("copy"); } catch (_) {}
        document.body.removeChild(ta);
        btn.textContent = "Copied ✓";
        setTimeout(() => { btn.textContent = "Copy"; }, 1500);
      }
    });
  });

  updateDownloadHref();
}

// ===========================================================================
// COCO Y0 comparison panel
// ===========================================================================
function initCocoComparePanel() {
  const pasteEl = document.getElementById("coco-paste");
  const runBtn = document.getElementById("coco-compare-btn");
  const csvLink = document.getElementById("coco-compare-csv-btn");
  const statusEl3 = document.getElementById("coco-compare-status");
  const outputEl = document.getElementById("coco-compare-output");
  const summaryEl = document.getElementById("coco-compare-summary");
  const tableBody = document.querySelector("#coco-compare-table tbody");
  const warningsEl = document.getElementById("coco-compare-warnings");
  const diagEl = document.getElementById("coco-parse-diagnostics");

  function setCmpStatus(text, kind) {
    statusEl3.textContent = text || "";
    statusEl3.className = "status-line" + (kind ? " " + kind : "");
  }

  function cell(value, agree) {
    const td = document.createElement("td");
    if (agree === true) { td.className = "agree-yes"; td.textContent = "✓"; }
    else if (agree === false) { td.className = "agree-no"; td.textContent = "✗"; }
    else { td.textContent = value; }
    return td;
  }

  // Render parser diagnostics (on both success and failure paths).
  // On failure: red banner + rejected-line listing.
  // On success: green banner with detected blocks, Rangsor/Y0 counts, winner rule.
  function renderDiagnostics({ ok, message, diagnostics, format_detected }) {
    diagEl.hidden = false;
    diagEl.className = "coco-diagnostics " + (ok ? "ok" : "error");

    const parts = [];
    if (message) parts.push(`<strong>${escapeHtml(message)}</strong>`);
    if (format_detected) {
      parts.push(`Detected format: <code>${escapeHtml(format_detected)}</code>`);
    }
    if (diagnostics) {
      const statsLine = [];
      if (diagnostics.blocks_detected && diagnostics.blocks_detected.length) {
        statsLine.push(
          `Blocks detected: ${diagnostics.blocks_detected
              .map(b => `<code>${escapeHtml(b)}</code>`).join(", ")}`);
      }
      if (typeof diagnostics.rangsor_count === "number") {
        statsLine.push(`Rangsor entries: <strong>${diagnostics.rangsor_count}</strong>`);
      }
      if (typeof diagnostics.y0_row_count === "number") {
        statsLine.push(`COCO:Y0 rows: <strong>${diagnostics.y0_row_count}</strong>`);
      }
      statsLine.push(
        `Mapped objects: <strong>${diagnostics.n_matched || 0}</strong>`);
      if (typeof diagnostics.n_rejected === "number") {
        statsLine.push(`Rejected: <strong>${diagnostics.n_rejected}</strong>`);
      }
      if (diagnostics.winner_rule) {
        statsLine.push(`Winner rule: <em>${escapeHtml(diagnostics.winner_rule)}</em>`);
      }
      if (statsLine.length) parts.push(statsLine.join(" · "));

      if (diagnostics.rejected_lines && diagnostics.rejected_lines.length) {
        const items = diagnostics.rejected_lines.slice(0, 10).map(r =>
          `<li>${r.line_no ? `line ${r.line_no}: ` : ""}` +
          `${escapeHtml(r.reason)}${r.text ? ` — <code>${escapeHtml(r.text)}</code>` : ""}</li>`
        ).join("");
        const overflow = diagnostics.rejected_lines.length > 10
          ? `<li>… and ${diagnostics.rejected_lines.length - 10} more</li>` : "";
        parts.push(`<ul class="rejected-list">${items}${overflow}</ul>`);
      }
    }
    diagEl.innerHTML = parts.join("<br>");
  }

  function clearDiagnostics() {
    diagEl.hidden = true;
    diagEl.innerHTML = "";
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, c => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    }[c]));
  }

  function renderComparison(data) {
    const s = data.summary;
    summaryEl.innerHTML =
      `Compared <strong>${s.n_images_compared}</strong> of ` +
      `${s.n_images_in_corpus} corpus images ` +
      `(${s.n_images_in_coco_paste} present in paste).<br>` +
      `Agreement rates: ` +
      `App vs TOPSIS <strong>${s.agree_app_vs_topsis.rate_pct}%</strong> ` +
      `(${s.agree_app_vs_topsis.count}/${s.n_images_compared}) · ` +
      `App vs COCO <strong>${s.agree_app_vs_coco.rate_pct}%</strong> ` +
      `(${s.agree_app_vs_coco.count}/${s.n_images_compared}) · ` +
      `TOPSIS vs COCO <strong>${s.agree_topsis_vs_coco.rate_pct}%</strong> ` +
      `(${s.agree_topsis_vs_coco.count}/${s.n_images_compared})`;

    tableBody.innerHTML = "";
    for (const r of data.rows) {
      const tr = document.createElement("tr");
      tr.appendChild(cell(r.image_id));
      tr.appendChild(cell(r.app_pick));
      tr.appendChild(cell(r.topsis_pick));
      tr.appendChild(cell(r.coco_pick));
      tr.appendChild(cell(null, r.app_vs_topsis_agree));
      tr.appendChild(cell(null, r.app_vs_coco_agree));
      tr.appendChild(cell(null, r.topsis_vs_coco_agree));
      tableBody.appendChild(tr);
    }

    warningsEl.innerHTML = "";
    if (data.warnings && data.warnings.length) {
      const ul = document.createElement("ul");
      data.warnings.forEach(w => {
        const li = document.createElement("li");
        li.textContent = w;
        ul.appendChild(li);
      });
      warningsEl.appendChild(document.createTextNode("Warnings:"));
      warningsEl.appendChild(ul);
      warningsEl.style.display = "";
    } else {
      warningsEl.style.display = "none";
    }
    outputEl.hidden = false;
  }

  async function runCompare() {
    const paste = pasteEl.value || "";
    if (!paste.trim()) {
      setCmpStatus("Paste the COCO Y0 output first.", "error");
      return;
    }
    setCmpStatus("Comparing…", "loading");
    clearDiagnostics();
    outputEl.hidden = true;
    csvLink.style.display = "none";

    try {
      const r = await fetch("/coco/compare", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ paste }),
      });

      if (!r.ok) {
        // Parser rejected the input. Show the full diagnostics so the user
        // can see WHY it was rejected and which lines were problematic.
        const err = await r.json().catch(() => ({}));
        const detailObj = err && err.detail;
        let message, diagnostics;
        if (detailObj && typeof detailObj === "object") {
          message = detailObj.message || "Parse failed.";
          diagnostics = detailObj.diagnostics;
        } else {
          message = typeof detailObj === "string" ? detailObj
                  : `Parse failed (${r.status}).`;
          diagnostics = null;
        }
        renderDiagnostics({ ok: false, message, diagnostics,
                            format_detected: null });
        setCmpStatus("Input rejected. See details below.", "error");
        return;
      }

      const data = await r.json();
      renderComparison(data);
      renderDiagnostics({
        ok: true,
        message: "COCO output accepted.",
        diagnostics: data.diagnostics,
        format_detected: data.format_detected,
      });

      csvLink.style.display = "";
      csvLink.textContent = "Download comparison as CSV";
      csvLink.href = "#";
      csvLink.onclick = async (e) => {
        e.preventDefault();
        const csvResp = await fetch("/coco/compare.csv", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ paste }),
        });
        if (!csvResp.ok) return;
        const blob = await csvResp.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = "coco_comparison.csv";
        document.body.appendChild(a);
        a.click();
        a.remove();
        URL.revokeObjectURL(url);
      };

      setCmpStatus("Done.", "");
    } catch (_) {
      setCmpStatus(STRINGS.errors.network, "error");
    }
  }

  runBtn.addEventListener("click", runCompare);
}
