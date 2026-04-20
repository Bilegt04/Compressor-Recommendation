// ===========================================================================
// Image Compression Recommender — frontend controller
//
// The native <input type="file"> is visually hidden. All user-visible text
// on the upload control is owned by this page, in English, regardless of
// the browser's OS locale. Interaction goes through the custom dropzone:
//   - click anywhere on the dropzone or the "Select Image" button
//   - drag a file onto the dropzone
//   - keyboard Enter/Space on the focused dropzone
// ===========================================================================

// UI strings — single source of truth. Everything shown to the user must
// come from here, never from a browser-localized element.
const STRINGS = {
  noFileSelected: "No file selected",
  dropPrompt: "Drop an image here, or click to browse",
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
const fileInput = document.getElementById("file");
const dropzone = document.getElementById("dropzone");
const selectBtn = document.getElementById("select-btn");
const filenameDisplay = document.getElementById("filename-display");
const dropzonePrimary = document.getElementById("dropzone-primary");
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

let configFlags = { oam_features_enabled: false, max_upload_bytes: 0 };

// ===========================================================================
// Bootstrap — fetch config to decide whether to show the dev panel
// ===========================================================================
(async function init() {
  try {
    const r = await fetch("/config");
    if (r.ok) configFlags = await r.json();
  } catch (_) { /* defaults are fine */ }

  if (configFlags.oam_features_enabled) {
    devPanel.hidden = false;
    devExports.innerHTML = `
      <a href="/exports/raw_results.csv" target="_blank" rel="noopener">raw_results.csv</a>
      <a href="/exports/attribute_dictionary.csv" target="_blank" rel="noopener">attribute_dictionary.csv</a>
      <a href="/exports/oam.csv?oam_variant=minimal" target="_blank" rel="noopener">oam.csv (minimal)</a>
      <a href="/exports/oam.csv?oam_variant=extended" target="_blank" rel="noopener">oam.csv (extended)</a>
      <a href="/exports/analysis.csv" target="_blank" rel="noopener">analysis.csv</a>
    `;

    // COCO Y0 panel — same gating.
    document.getElementById("coco-panel").hidden = false;
    initCocoPanel();
  }

  // Initial label — guarantees English even before any interaction.
  filenameDisplay.textContent = STRINGS.noFileSelected;
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

function isAcceptedFile(file) {
  if (!file) return false;
  // Prefer MIME type; fall back to extension if the browser reports empty/odd.
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

// Set a single file on the <input> via DataTransfer so form submission
// still sends it the normal way. Supported in all modern browsers.
function attachFileToInput(file) {
  const dt = new DataTransfer();
  dt.items.add(file);
  fileInput.files = dt.files;
}

function applyFileSelection(file) {
  if (!isAcceptedFile(file)) {
    setStatus(STRINGS.errors.wrongType, "error");
    clearFileSelection();
    return false;
  }
  const sizeErr = validateFileSize(file);
  if (sizeErr) {
    setStatus(sizeErr, "error");
    clearFileSelection();
    return false;
  }

  attachFileToInput(file);
  filenameDisplay.textContent = file.name;
  dropzone.classList.add("has-file");
  submitBtn.disabled = false;
  setStatus("", "");
  return true;
}

function clearFileSelection() {
  fileInput.value = "";
  filenameDisplay.textContent = STRINGS.noFileSelected;
  dropzone.classList.remove("has-file");
  submitBtn.disabled = true;
}

// ===========================================================================
// Dropzone interactions
// ===========================================================================

// Clicking the dropzone or the Select Image button opens the file picker.
dropzone.addEventListener("click", (e) => {
  // Avoid double-trigger when clicking the nested button (which also opens
  // the picker via its own handler below).
  if (e.target === selectBtn) return;
  fileInput.click();
});
selectBtn.addEventListener("click", (e) => {
  e.stopPropagation();
  fileInput.click();
});

// Keyboard — Enter/Space on the focused dropzone opens the picker.
dropzone.addEventListener("keydown", (e) => {
  if (e.key === "Enter" || e.key === " ") {
    e.preventDefault();
    fileInput.click();
  }
});

// Native change event — user picked a file via the picker.
fileInput.addEventListener("change", () => {
  const file = fileInput.files && fileInput.files[0];
  if (file) applyFileSelection(file);
});

// --- Drag and drop ---
["dragenter", "dragover"].forEach(evt => {
  dropzone.addEventListener(evt, (e) => {
    e.preventDefault();
    e.stopPropagation();
    dropzone.classList.add("is-dragover");
    dropzonePrimary.textContent = STRINGS.dropActive;
  });
});
["dragleave", "dragend"].forEach(evt => {
  dropzone.addEventListener(evt, (e) => {
    e.preventDefault();
    e.stopPropagation();
    dropzone.classList.remove("is-dragover");
    dropzonePrimary.textContent = STRINGS.dropPrompt;
  });
});
dropzone.addEventListener("drop", (e) => {
  e.preventDefault();
  e.stopPropagation();
  dropzone.classList.remove("is-dragover");
  dropzonePrimary.textContent = STRINGS.dropPrompt;

  const files = e.dataTransfer && e.dataTransfer.files;
  if (!files || files.length === 0) return;
  if (files.length > 1) {
    setStatus(STRINGS.errors.tooManyFiles, "error");
    return;
  }
  applyFileSelection(files[0]);
});

// Prevent the window from navigating away when a file is dropped outside
// the dropzone (default browser behavior is to open the file).
window.addEventListener("dragover", (e) => e.preventDefault());
window.addEventListener("drop", (e) => e.preventDefault());

// ===========================================================================
// Rendering
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
      <div class="stat-row">
        <span class="stat-label">File size</span>
        <span class="stat-value">${formatKb(v.compressed_size_kb)}</span>
      </div>
      <div class="stat-row">
        <span class="stat-label">Saved</span>
        <span class="stat-value">${formatPercent(v.percent_saved)}</span>
      </div>
      <div class="stat-row">
        <span class="stat-label" title="Peak signal-to-noise ratio — a technical quality measure. Higher is better.">PSNR</span>
        <span class="stat-value">${typeof v.psnr === "number" ? v.psnr.toFixed(2) + " dB" : "—"}</span>
      </div>
      <div class="stat-row">
        <span class="stat-label" title="Structural similarity to the original. 1.0 means identical.">SSIM</span>
        <span class="stat-value">${typeof v.ssim === "number" ? v.ssim.toFixed(4) : "—"}</span>
      </div>
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

  const file = fileInput.files && fileInput.files[0];
  if (!file) {
    setStatus(STRINGS.errors.noFile, "error");
    return;
  }

  submitBtn.disabled = true;
  setStatus(STRINGS.analyzing, "loading");

  const fd = new FormData();
  fd.append("file", file);

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
    submitBtn.disabled = !(fileInput.files && fileInput.files[0]);
  }
});

// ===========================================================================
// COCO Y0 panel (gated by ENABLE_OAM_FEATURES)
//
// The external MIAU COCO Y0 solver
// (https://miau.my-x.hu/myx-free/coco/beker_y0.php) is a manual external
// tool. This panel produces text the user copies by hand. There is no
// automated submission and no scraping of the external site.
// ===========================================================================
function initCocoPanel() {
  const variantSel = document.getElementById("coco-variant");
  const stepInput = document.getElementById("coco-step-count");
  const buildBtn = document.getElementById("coco-build-btn");
  const statusEl = document.getElementById("coco-status");
  const outputBox = document.getElementById("coco-output");
  const summaryEl = document.getElementById("coco-summary");
  const matrixEl = document.getElementById("coco-matrix");
  const objectsEl = document.getElementById("coco-objects");
  const attributesEl = document.getElementById("coco-attributes");
  const downloadBtn = document.getElementById("coco-download-btn");

  function setCocoStatus(text, kind) {
    statusEl.textContent = text || "";
    statusEl.className = "status-line" + (kind ? " " + kind : "");
  }

  function updateDownloadHref() {
    const v = encodeURIComponent(variantSel.value);
    const s = encodeURIComponent(stepInput.value || "0");
    downloadBtn.href = `/coco/download?oam_variant=${v}&step_count=${s}`;
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
      matrixEl.textContent = data.matrix_text;
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

  // Copy buttons — wire each to its target <pre>.
  document.querySelectorAll(".copy-btn").forEach(btn => {
    btn.addEventListener("click", async () => {
      const targetId = btn.getAttribute("data-target");
      const target = document.getElementById(targetId);
      if (!target) return;
      const text = target.textContent || "";
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
        // Fallback for browsers without clipboard API permission
        // (e.g., insecure context). Select the text manually.
        const range = document.createRange();
        range.selectNodeContents(target);
        const sel = window.getSelection();
        sel.removeAllRanges();
        sel.addRange(range);
        btn.textContent = "Select + Ctrl/⌘ C";
        setTimeout(() => { btn.textContent = "Copy"; }, 2000);
      }
    });
  });

  updateDownloadHref();
}
