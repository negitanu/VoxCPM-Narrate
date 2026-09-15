(() => {
  const $ = (id) => document.getElementById(id);

  const form = $("job-form");
  const submitBtn = $("submit-btn");
  const statusChip = $("status-chip");
  const progressBar = $("progress-bar");
  const progressMeta = $("progress-meta");
  const progressMessage = $("progress-message");
  const resultPanel = $("result-panel");
  const resultAudio = $("result-audio");
  const downloadBtn = $("download-btn");
  const resultStats = $("result-stats");
  const refInput = $("reference");
  const ssmlInput = $("ssml");
  const ssmlText = $("ssml-text");
  const refPreview = $("ref-preview");

  let pollTimer = null;
  let objectUrls = [];

  function revokeUrls() {
    objectUrls.forEach((u) => URL.revokeObjectURL(u));
    objectUrls = [];
  }

  function setChip(status, label) {
    statusChip.className = `status-chip ${status || ""}`;
    statusChip.textContent = label;
  }

  function wireDropzone(zoneId, input) {
    const zone = $(zoneId);
    zone.addEventListener("click", () => input.click());
    zone.addEventListener("dragover", (e) => {
      e.preventDefault();
      zone.classList.add("dragover");
    });
    zone.addEventListener("dragleave", () => zone.classList.remove("dragover"));
    zone.addEventListener("drop", (e) => {
      e.preventDefault();
      zone.classList.remove("dragover");
      if (e.dataTransfer.files?.length) {
        input.files = e.dataTransfer.files;
        input.dispatchEvent(new Event("change"));
      }
    });
  }

  wireDropzone("ref-dropzone", refInput);
  wireDropzone("ssml-dropzone", ssmlInput);

  refInput.addEventListener("change", () => {
    const file = refInput.files?.[0];
    $("ref-filename").textContent = file ? file.name : "未選択";
    if (file) {
      const url = URL.createObjectURL(file);
      objectUrls.push(url);
      refPreview.src = url;
      refPreview.classList.remove("hidden");
    } else {
      refPreview.classList.add("hidden");
    }
  });

  ssmlInput.addEventListener("change", async () => {
    const file = ssmlInput.files?.[0];
    $("ssml-filename").textContent = file ? file.name : "未選択";
    if (file) {
      ssmlText.value = await file.text();
    }
  });

  async function checkHealth() {
    try {
      const res = await fetch("/api/health");
      const data = await res.json();
      $("health").textContent = data.ok ? "API online" : "API degraded";
    } catch {
      $("health").textContent = "API offline";
    }
  }

  function stopPoll() {
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  function renderJob(job) {
    const pct = job.percent || 0;
    progressBar.style.width = `${pct}%`;
    progressMeta.textContent = job.total
      ? `${job.current || 0} / ${job.total} · ${pct}% · ${job.phase}`
      : `${job.phase || job.status} · ${pct}%`;
    progressMessage.textContent = job.message || "";

    if (job.status === "queued") setChip("running", "キュー待ち");
    else if (job.status === "running") setChip("running", "生成中");
    else if (job.status === "improving") setChip("improving", "自己改善中");
    else if (job.status === "done") setChip("done", "完了");
    else if (job.status === "error") setChip("error", "エラー");

    if (job.status === "done" && job.download_url) {
      resultPanel.classList.remove("hidden");
      resultAudio.src = job.download_url;
      downloadBtn.href = job.download_url;
      const mins = job.duration_sec ? (job.duration_sec / 60).toFixed(1) : "?";
      const improve = job.improve_summary
        ? ` · 改善: checked ${job.improve_summary.checked}, replaced ${job.improve_summary.replaced}`
        : "";
      resultStats.textContent = `セグメント ${job.segment_count ?? "?"} · 約 ${mins} 分${improve}`;
      submitBtn.disabled = false;
      stopPoll();
    }

    if (job.status === "error") {
      progressMessage.textContent = job.error || job.message || "失敗しました";
      submitBtn.disabled = false;
      stopPoll();
    }
  }

  async function pollJob(jobId) {
    const res = await fetch(`/api/jobs/${jobId}`);
    if (!res.ok) throw new Error("ジョブ状態の取得に失敗しました");
    const job = await res.json();
    renderJob(job);
  }

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    revokeUrls();
    resultPanel.classList.add("hidden");

    const refFile = refInput.files?.[0];
    if (!refFile) {
      alert("参照声音ファイルを選択してください");
      return;
    }

    let ssmlFile = ssmlInput.files?.[0];
    const pasted = ssmlText.value.trim();
    if (!ssmlFile && !pasted) {
      alert("SSML ファイルを選ぶか、テキストを貼り付けてください");
      return;
    }
    if (!ssmlFile && pasted) {
      ssmlFile = new File([pasted], "script.ssml", { type: "application/xml" });
    }

    const body = new FormData();
    body.append("reference", refFile);
    body.append("ssml", ssmlFile);
    body.append("device", $("device").value);
    body.append("control", $("control").value);
    body.append("max_chars", $("max_chars").value);
    body.append("cfg_value", $("cfg_value").value);
    body.append("timesteps", "10");
    body.append("seed", "42");
    body.append("improve", $("improve").checked ? "true" : "false");
    body.append("improve_asr", $("improve_asr").checked ? "true" : "false");
    body.append("improve_rounds", $("improve_rounds").value);

    submitBtn.disabled = true;
    setChip("running", "送信中");
    progressBar.style.width = "2%";
    progressMeta.textContent = "ジョブ作成中…";
    progressMessage.textContent = "アップロードしています";

    try {
      const res = await fetch("/api/jobs", { method: "POST", body });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `HTTP ${res.status}`);
      }
      const job = await res.json();
      renderJob(job);
      stopPoll();
      pollTimer = setInterval(() => {
        pollJob(job.id).catch((err) => {
          progressMessage.textContent = String(err);
        });
      }, 2000);
      await pollJob(job.id);
    } catch (err) {
      setChip("error", "エラー");
      progressMessage.textContent = String(err);
      submitBtn.disabled = false;
    }
  });

  checkHealth();
})();
