// ==============================
// TAB SWITCHING
// ==============================
document.querySelectorAll(".tab-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab-btn").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".tab-panel").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    document.getElementById(`tab-${btn.dataset.tab}`).classList.add("active");
  });
});

// ==============================
// SIZE OPTION TOGGLES
// ==============================
function setupSizeGroup(groupName, customWrapId) {
  const group = document.querySelector(`[data-group="${groupName}"]`);
  const customWrap = document.getElementById(customWrapId);
  group.querySelectorAll(".size-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      group.querySelectorAll(".size-btn").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      customWrap.style.display = btn.dataset.value === "custom" ? "block" : "none";
    });
  });
}
setupSizeGroup("video-size", "video-custom-wrap");
setupSizeGroup("image-size", "image-custom-wrap");
setupSizeGroup("batch-video-size", "batch-video-custom-wrap");
setupSizeGroup("batch-image-size", "batch-image-custom-wrap");

function getActiveSize(groupName) {
  return document.querySelector(`[data-group="${groupName}"] .size-btn.active`).dataset.value;
}

// ==============================
// DROPZONE HELPERS
// ==============================
function setupDropzone(dropId, inputId, { multiple = false, onFiles } = {}) {
  const drop = document.getElementById(dropId);
  const input = document.getElementById(inputId);

  drop.addEventListener("click", () => input.click());
  input.addEventListener("change", () => onFiles(input.files));

  ["dragover", "dragenter"].forEach((evt) =>
    drop.addEventListener(evt, (e) => {
      e.preventDefault();
      drop.classList.add("has-file");
    })
  );
  drop.addEventListener("drop", (e) => {
    e.preventDefault();
    const files = e.dataTransfer.files;
    if (files.length) {
      input.files = files;
      onFiles(files);
    }
  });
}

function previewSingleFile(drop, file, kind) {
  drop.classList.add("has-file");
  drop.innerHTML = "";
  const el = document.createElement(kind === "video" ? "video" : "img");
  el.src = URL.createObjectURL(file);
  if (kind === "video") {
    el.controls = true;
    el.muted = true;
  }
  drop.appendChild(el);

  // Re-attach the (now-replaced) file input so future clicks still work.
  const input = document.createElement("input");
  input.type = "file";
  input.id = drop.id === "video-drop" ? "video-input" : "image-input";
  input.accept = kind === "video" ? "video/*" : "image/*";
  input.style.display = "none";
  drop.appendChild(input);
}

// ==============================
// JOB POLLING
// ==============================
async function pollJob(jobId, { onProgress, onDone, onError }) {
  const poll = async () => {
    let res;
    try {
      res = await fetch(`/api/jobs/${jobId}`);
    } catch (e) {
      onError("Network error while checking job status.");
      return;
    }
    if (!res.ok) {
      onError("Job not found.");
      return;
    }
    const job = await res.json();

    if (job.status === "running") {
      onProgress(job.progress || 0, job.desc || "");
      setTimeout(poll, 1000);
    } else if (job.status === "done") {
      onProgress(1, job.desc || "Done");
      onDone(job.result);
    } else {
      onError(job.error || "Compression failed.");
    }
  };
  poll();
}

function setProgress(prefix, active, frac, desc) {
  const wrap = document.getElementById(`${prefix}-progress`);
  const fill = document.getElementById(`${prefix}-progress-fill`);
  const descEl = document.getElementById(`${prefix}-progress-desc`);
  wrap.classList.toggle("active", active);
  if (active) {
    fill.style.width = `${Math.round(frac * 100)}%`;
    descEl.textContent = desc;
  }
}

function setStatus(prefix, active, html, isError) {
  const box = document.getElementById(`${prefix}-status`);
  box.classList.toggle("active", active);
  box.classList.toggle("error", !!isError);
  box.innerHTML = html;
}

// ==============================
// SINGLE VIDEO
// ==============================
let videoFile = null;

setupDropzone("video-drop", "video-input", {
  onFiles: (files) => {
    if (!files.length) return;
    videoFile = files[0];
    previewSingleFile(document.getElementById("video-drop"), videoFile, "video");
  },
});

document.getElementById("video-compress-btn").addEventListener("click", async () => {
  if (!videoFile) {
    alert("Please choose a video first.");
    return;
  }
  const btn = document.getElementById("video-compress-btn");
  btn.disabled = true;
  setStatus("video", false, "");
  document.getElementById("video-downloads").innerHTML = "";
  setProgress("video", true, 0, "Uploading...");

  const sizeChoice = getActiveSize("video-size");
  const customMb = document.getElementById("video-custom-mb").value;

  const form = new FormData();
  form.append("file", videoFile);
  form.append("size_choice", sizeChoice);
  if (sizeChoice === "custom") form.append("custom_mb", customMb);

  try {
    const res = await fetch("/api/compress/video", { method: "POST", body: form });
    if (!res.ok) throw new Error((await res.json()).detail || "Upload failed.");
    const { job_id } = await res.json();

    pollJob(job_id, {
      onProgress: (frac, desc) => setProgress("video", true, frac, desc),
      onDone: (result) => {
        setProgress("video", false, 1, "");
        btn.disabled = false;

        const outBox = document.getElementById("video-output-box");
        outBox.innerHTML = `<video src="${result.download_url}" controls muted></video>`;

        const cloudLine = result.cloud_url
          ? `<div class="stat"><span>Cloudinary (permanent)</span><a href="${result.cloud_url}" target="_blank">Open ↗</a></div>`
          : `<div class="stat"><span>Cloudinary</span><span>skipped - ${result.cloud_error || "not configured"}</span></div>`;

        setStatus("video", true, `
          <div class="stat"><span>Original Size</span><span>${result.original_size_mb} MB</span></div>
          <div class="stat"><span>Final Size</span><span>${result.final_size_mb} MB</span></div>
          <div class="stat"><span>Size Reduction</span><span>${result.reduction_pct}%</span></div>
          <div class="stat"><span>Duration</span><span>${result.duration}</span></div>
          <div class="stat"><span>Video / Audio Bitrate</span><span>${result.video_kbps} / ${result.audio_kbps} kbps</span></div>
          <div class="stat"><span>Resolution</span><span>${result.original_resolution} → ${result.output_resolution}</span></div>
          <div class="stat"><span>Encoding Passes</span><span>${result.attempts}</span></div>
          ${cloudLine}
        `);

        document.getElementById("video-downloads").innerHTML =
          `<a class="btn-secondary" href="${result.download_url}" download>⬇ Download Compressed Video</a>`;
      },
      onError: (err) => {
        setProgress("video", false, 0, "");
        setStatus("video", true, err, true);
        btn.disabled = false;
      },
    });
  } catch (e) {
    setProgress("video", false, 0, "");
    setStatus("video", true, e.message, true);
    btn.disabled = false;
  }
});

// ==============================
// SINGLE IMAGE
// ==============================
let imageFile = null;

setupDropzone("image-drop", "image-input", {
  onFiles: (files) => {
    if (!files.length) return;
    imageFile = files[0];
    previewSingleFile(document.getElementById("image-drop"), imageFile, "image");
  },
});

document.getElementById("image-compress-btn").addEventListener("click", async () => {
  if (!imageFile) {
    alert("Please choose an image first.");
    return;
  }
  const btn = document.getElementById("image-compress-btn");
  btn.disabled = true;
  setStatus("image", false, "");
  document.getElementById("image-downloads").innerHTML = "";
  setProgress("image", true, 0, "Uploading...");

  const sizeChoice = getActiveSize("image-size");
  const customKb = document.getElementById("image-custom-kb").value;

  const form = new FormData();
  form.append("file", imageFile);
  form.append("size_choice", sizeChoice);
  if (sizeChoice === "custom") form.append("custom_kb", customKb);

  try {
    const res = await fetch("/api/compress/image", { method: "POST", body: form });
    if (!res.ok) throw new Error((await res.json()).detail || "Upload failed.");
    const { job_id } = await res.json();

    pollJob(job_id, {
      onProgress: (frac, desc) => setProgress("image", true, frac, desc),
      onDone: (result) => {
        setProgress("image", false, 1, "");
        btn.disabled = false;

        const outBox = document.getElementById("image-output-box");
        outBox.innerHTML = `<img src="${result.download_url}">`;

        const cloudLine = result.cloud_url
          ? `<div class="stat"><span>Cloudinary (permanent)</span><a href="${result.cloud_url}" target="_blank">Open ↗</a></div>`
          : `<div class="stat"><span>Cloudinary</span><span>skipped - ${result.cloud_error || "not configured"}</span></div>`;

        setStatus("image", true, `
          <div class="stat"><span>Format</span><span>${result.format}</span></div>
          <div class="stat"><span>Original Size</span><span>${result.original_size_kb} KB</span></div>
          <div class="stat"><span>Final Size</span><span>${result.final_size_kb} KB</span></div>
          <div class="stat"><span>Size Reduction</span><span>${result.reduction_pct}%</span></div>
          <div class="stat"><span>Quality Used</span><span>${result.quality}</span></div>
          <div class="stat"><span>Resolution</span><span>${result.original_resolution} → ${result.output_resolution}</span></div>
          ${cloudLine}
        `);

        document.getElementById("image-downloads").innerHTML =
          `<a class="btn-secondary" href="${result.download_url}" download>⬇ Download Compressed Image</a>`;
      },
      onError: (err) => {
        setProgress("image", false, 0, "");
        setStatus("image", true, err, true);
        btn.disabled = false;
      },
    });
  } catch (e) {
    setProgress("image", false, 0, "");
    setStatus("image", true, e.message, true);
    btn.disabled = false;
  }
});

// ==============================
// BATCH (shared logic for video + image)
// ==============================
function setupBatch(kind, unit) {
  const files = { list: [] };
  const dropId = `batch-${kind}-drop`;
  const inputId = `batch-${kind}-input`;
  const countId = `batch-${kind}-count`;
  const textId = `batch-${kind}-drop-text`;

  setupDropzone(dropId, inputId, {
    onFiles: (fileList) => {
      files.list = Array.from(fileList);
      document.getElementById(countId).textContent = `${files.list.length} file(s) selected`;
      document.getElementById(textId).textContent = `${files.list.length} file(s) ready - click to change`;
    },
  });

  document.getElementById(`batch-${kind}-compress-btn`).addEventListener("click", async () => {
    if (!files.list.length) {
      alert(`Please choose ${kind}s first.`);
      return;
    }
    const btn = document.getElementById(`batch-${kind}-compress-btn`);
    btn.disabled = true;
    setStatus(`batch-${kind}`, false, "");
    document.getElementById(`batch-${kind}-list`).innerHTML = "";
    document.getElementById(`batch-${kind}-downloads`).innerHTML = "";
    setProgress(`batch-${kind}`, true, 0, "Uploading...");

    const sizeChoice = getActiveSize(`batch-${kind}-size`);
    const customVal = document.getElementById(`batch-${kind}-custom-${unit}`).value;

    const form = new FormData();
    files.list.forEach((f) => form.append("files", f));
    form.append("size_choice", sizeChoice);
    if (sizeChoice === "custom") form.append(`custom_${unit}`, customVal);

    try {
      const res = await fetch(`/api/compress/${kind}/batch`, { method: "POST", body: form });
      if (!res.ok) throw new Error((await res.json()).detail || "Upload failed.");
      const { job_id } = await res.json();

      pollJob(job_id, {
        onProgress: (frac, desc) => setProgress(`batch-${kind}`, true, frac, desc),
        onDone: (result) => {
          setProgress(`batch-${kind}`, false, 1, "");
          btn.disabled = false;

          const sizeUnit = unit === "mb" ? "MB" : "KB";
          const sizeKey = unit === "mb" ? "size_mb" : "size_kb";

          const listHtml = result.files
            .map((r) => {
              if (r.success) {
                const orig = r[`original_${sizeKey}`];
                const final = r[`final_${sizeKey}`];
                return `<div class="batch-item ok"><span class="name">${r.name}</span><span class="detail">${r.action} - ${orig} → ${final} ${sizeUnit}</span></div>`;
              }
              return `<div class="batch-item fail"><span class="name">${r.name}</span><span class="detail">FAILED - ${r.error}</span></div>`;
            })
            .join("");
          document.getElementById(`batch-${kind}-list`).innerHTML = listHtml;

          setStatus(`batch-${kind}`, true, `
            <div class="stat"><span>Total</span><span>${result.files.length}</span></div>
            <div class="stat"><span>Succeeded</span><span>${result.success_count}</span></div>
            <div class="stat"><span>Failed</span><span>${result.failed_count}</span></div>
          `);

          if (result.zip_download_url) {
            document.getElementById(`batch-${kind}-downloads`).innerHTML =
              `<a class="btn-secondary" href="${result.zip_download_url}" download>⬇ Download All as ZIP</a>`;
          }
        },
        onError: (err) => {
          setProgress(`batch-${kind}`, false, 0, "");
          setStatus(`batch-${kind}`, true, err, true);
          btn.disabled = false;
        },
      });
    } catch (e) {
      setProgress(`batch-${kind}`, false, 0, "");
      setStatus(`batch-${kind}`, true, e.message, true);
      btn.disabled = false;
    }
  });
}
setupBatch("video", "mb");
setupBatch("image", "kb");
