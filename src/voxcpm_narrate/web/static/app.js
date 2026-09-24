(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const activeStates = new Set(["queued", "running", "improving"]);
  const labels = {draft:"下書き",queued:"待機中",running:"処理中",improving:"評価中",done:"保存済み",interrupted:"中断",error:"エラー"};
  let job = null, selectedId = null, renderedId = null, polling = false, sequence = [], librarySignature = "";
  let settings = {}, requestBusy = false;
  let lexiconRevision = 0, renameBase = "";
  const dictionaryText = entries => Object.entries(entries).map(([k,v]) => `${k}=${v}`).join("\n");
  const checked = new Set();
  const draftKey = (jid, sid) => `voxcpm.edit.${jid}.${sid}`;
  function readLocal(key) { try { return JSON.parse(localStorage.getItem(key)); } catch { return null; } }
  function store(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch { notice("ブラウザの一時保存ができません。編集をサーバーに保存してください", true); } }
  function notice(message, error = false) { $("notice").textContent = message; $("notice").className = `notice${error ? " error" : ""}${message ? "" : " hidden"}`; }
  async function api(url, options = {}) {
    const res = await fetch(url, options);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const detail = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail || "操作に失敗しました");
      throw new Error(detail);
    }
    return data;
  }
  const post = (url, data = {}) => api(url, {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(data)});
  async function action(fn) {
    if (requestBusy) return;
    requestBusy = true;
    try { notice(""); await fn(); } catch (err) { notice(err.message, true); }
    finally { requestBusy = false; if(job) render(job); }
  }
  const current = () => job?.segments.find(s => s.id === selectedId);
  const accepted = (s) => s.versions.find(v => v.id === s.accepted);
  const busy = () => job && activeStates.has(job.status);
  const audioUrl = (sid, vid) => `/api/jobs/${job.id}/segments/${encodeURIComponent(sid)}?version_id=${encodeURIComponent(vid)}`;
  const request = () => ({request_id:crypto.randomUUID(),api_key:$("api-key").value.trim()});
  const provider = () => job?.config?.llm_provider || $("llm-provider").value;
  const audioJudgeModel = () => provider() === "azure" ? $("azure-audio-deployment").value.trim() : $("llm-model").value;
  const config = () => ({device:$("device").value,control:$("control").value,max_chars:+$("max-chars").value,
    convert_numbers:$("convert-numbers").checked,
    number_reading_style:$("number-reading-style").value,
    pace_mode:$("pace-mode").value,target_mora_rate:+$("target-mora-rate").value,
    cfg_value:+$("cfg").value,timesteps:+$("timesteps").value,seed:+$("seed").value,
    improve:$("improve").checked,improve_asr:$("asr").checked,improve_llm:$("llm").checked,
    improve_rounds:+$("rounds").value,llm_provider:$("llm-provider").value,
    llm_model:$("llm-provider").value === "azure" ? ($("azure-text-deployment").value.trim() || $("azure-audio-deployment").value.trim()) : $("llm-model").value || settings.default_model,
    audio_judge_model:$("llm-provider").value === "azure" ? $("azure-audio-deployment").value.trim() : $("llm-model").value});

  async function refreshLibrary() {
    const trash = $("show-trash").checked;
    const data = await api(`/api/jobs?deleted=${trash}`);
    $("empty-trash").classList.toggle("hidden", !trash);
    $("empty-trash").disabled = !data.jobs.length;
    const signature = JSON.stringify(data.jobs) + (job?.id || "") + trash;
    if(signature === librarySignature) return;
    librarySignature = signature;
    $("library").replaceChildren(...data.jobs.map(j => {
      const b = document.createElement("button"); b.className = "library-item" + (job?.id === j.id ? " active" : "");
      const title = document.createElement("span"); title.textContent = j.title;
      const meta = document.createElement("small"); meta.textContent = `${labels[j.status] || j.status} · ${j.ready}/${j.total}`;
      b.append(title,meta); b.onclick = () => action(() => openJob(j.id)); b.disabled = trash;
      const row = document.createElement("div"); row.className = "library-row";
      const remove = document.createElement("button"); remove.className = "library-delete";
      remove.textContent = trash ? "復元" : "削除";
      remove.setAttribute("aria-label", `${j.title}を${trash ? "復元" : "削除"}`);
      remove.disabled = activeStates.has(j.status) || !!j.purging;
      if (j.purging) meta.textContent = "完全削除の途中・再実行してください";
      remove.onclick = () => action(async () => {
        if (trash) { await post(`/api/jobs/${j.id}/restore`); }
        else {
          if (!confirm(`「${j.title}」をごみ箱へ移動しますか？音声は保持され、復元できます。`)) return;
          await api(`/api/jobs/${j.id}`, {method:"DELETE"});
          if (job?.id === j.id) { $("new-job").click(); $("audio").removeAttribute("src"); }
        }
        librarySignature = ""; await refreshLibrary();
        notice(trash ? "制作を復元しました。ごみ箱表示を解除すると開けます。" : "ごみ箱へ移動しました。ごみ箱から復元できます。");
      });
      row.append(b,remove); return row;
    }));
  }
  $("empty-trash").onclick = () => action(async () => {
    const {jobs} = await api("/api/jobs?deleted=true");
    if (!jobs.length) { await refreshLibrary(); return; }
    if (!confirm(`ごみ箱の${jobs.length}件を完全に削除しますか？台本・参照音声・生成音声・書き出しファイルが削除され、復元できません。`)) return;
    try {
      const result = await post("/api/trash/empty", {job_ids:jobs.map(j=>j.id),confirmed:true});
      for (const jid of result.deleted_ids) {
        for (const key of Object.keys(localStorage)) {
          if (key.startsWith(`voxcpm.edit.${jid}.`) || key.startsWith(`voxcpm.reading.${jid}.`)) localStorage.removeItem(key);
        }
      }
      notice(`${result.deleted_ids.length}件を完全削除しました。` + (result.failed_ids.length ? `${result.failed_ids.length}件は削除できませんでした。ファイルへのアクセス権を確認して再実行してください。` : ""), !!result.failed_ids.length);
    } finally { librarySignature = ""; await refreshLibrary(); }
  });
  $("show-trash").onchange = () => action(refreshLibrary);
  async function openJob(id) {
    window.referenceRecorder.cancel();
    closeRename();
    const data = await api(`/api/jobs/${encodeURIComponent(id)}`);
    sequence = []; $("audio").pause(); checked.clear(); selectedId = data.segments[0]?.id; renderedId = null;
    job = data; history.replaceState(null,"",`?job=${id}`);
    const savedProvider=data.config.llm_provider || "openrouter";
    if($("llm-provider").value !== savedProvider)$("api-key").value="";
    $("llm-provider").value=savedProvider;
    if(savedProvider === "azure") {
      $("azure-text-deployment").value=data.config.llm_model || "";
      $("azure-audio-deployment").value=data.config.audio_judge_model || settings.azure_audio_deployment || "";
    } else if([...$("llm-model").options].some(o=>o.value===data.config.audio_judge_model)) {
      $("llm-model").value=data.config.audio_judge_model;
    }
    updateProviderUI();
    $("setup").classList.add("hidden"); $("studio").classList.remove("hidden");
    $("dictionary").value = Object.entries(data.dictionary).map(([k,v]) => `${k}=${v}`).join("\n");
    $("pronunciation-candidates").replaceChildren();
    render(data); await refreshLibrary(); await loadPronunciations();
  }
  function closeRename() {
    $("rename-form").classList.add("hidden");
    $("rename-job").setAttribute("aria-expanded", "false");
  }
  $("rename-job").onclick = () => {
    renameBase = job.title;
    $("rename-title").value = job.title;
    $("rename-form").classList.remove("hidden");
    $("rename-job").setAttribute("aria-expanded", "true");
    $("rename-title").focus(); $("rename-title").select();
  };
  $("cancel-rename").onclick = closeRename;
  $("rename-form").onsubmit = event => {
    event.preventDefault();
    action(async () => {
      const title = $("rename-title").value.trim();
      if (!title) throw new Error("制作名を入力してください。");
      const jid = job.id;
      const data = await api(`/api/jobs/${jid}/title`, {method:"PATCH",headers:{"Content-Type":"application/json"},body:JSON.stringify({title,expected_title:renameBase})});
      if (job?.id === jid) { render(data); closeRename(); }
      await refreshLibrary(); notice("制作名を変更しました。");
    });
  };
  function editValue() {
    return {text:$("edit-text").value,reading:$("edit-reading").value,control:$("edit-control").value,pause_before_sec:+$("edit-pause").value,convert_numbers:$("edit-convert-numbers").value === "" ? null : $("edit-convert-numbers").value === "true",number_reading_style:$("edit-number-reading-style").value || null};
  }
  function trackEdit() {
    const seg = current(); if(!seg) return;
    const prior = readLocal(draftKey(job.id,seg.id));
    store(draftKey(job.id,seg.id), {draft:editValue(),revision:prior?.revision ?? seg.revision,base:prior?.base ?? seg.draft});
    $("save-status").textContent = "未保存（このブラウザに一時保存）";
  }
  ["edit-text","edit-reading","edit-control","edit-pause","edit-convert-numbers","edit-number-reading-style"].forEach(id => $(id).addEventListener("input", trackEdit));
  async function saveEdit() {
    const seg = current(); if(!seg) return;
    const local = readLocal(draftKey(job.id,seg.id)); if(!local) return;
    if(busy()) throw new Error("処理が終わってから編集を保存してください。入力内容は保持されています。");
    if(local.revision !== seg.revision && local.base && JSON.stringify(local.base) === JSON.stringify(seg.draft)) local.revision = seg.revision;
    const data = await api(`/api/jobs/${job.id}/segments/${seg.id}`,{method:"PATCH",headers:{"Content-Type":"application/json"},body:JSON.stringify({expected_revision:local.revision,draft:local.draft})});
    localStorage.removeItem(draftKey(job.id,seg.id)); renderedId = null; render(data);
    notice("編集を保存しました。音声に反映するには候補を再生成してください。");
  }
  function fillEditor(seg) {
    const local = readLocal(draftKey(job.id,seg.id)); const draft = local?.draft || seg.draft;
    $("edit-text").value = draft.text; $("edit-reading").value = draft.reading;
    $("edit-convert-numbers").value = draft.convert_numbers == null ? "" : String(draft.convert_numbers);
    $("edit-number-reading-style").value = draft.number_reading_style || "";
    $("edit-number-reading-style").options[0].textContent = `制作の設定に従う（${job.config.number_reading_style === "kanji" ? "漢数字" : "ひらがな"}）`;
    $("edit-convert-numbers").options[0].textContent = `制作の設定に従う（${job.config.convert_numbers !== false ? "変換する" : "変換しない"}）`;
    $("edit-control").value = draft.control; $("edit-pause").value = draft.pause_before_sec;
    $("save-status").textContent = local ? (local.revision === seg.revision ? "未保存（ブラウザに保持）" : "競合：入力を保持しています。再読み込み前にコピーしてください") : "保存済み";
    $("resolve-edit").classList.toggle("hidden",!local || local.revision === seg.revision);
    renderedId = `${job.id}/${seg.id}/${seg.revision}`;
  }
  function renderSegments() {
    const panel = $("segments"), filter = $("filter").value;
    for(const seg of job.segments) {
      let row = panel.querySelector(`[data-id="${seg.id}"]`);
      if(!row) {
        row = document.createElement("div"); row.className = "segment-row"; row.dataset.id = seg.id;
        const c = document.createElement("input"); c.type = "checkbox"; c.setAttribute("aria-label",`${seg.id} を生成対象に選択`);
        c.onchange = () => c.checked ? checked.add(seg.id) : checked.delete(seg.id);
        const b = document.createElement("button"); b.innerHTML = "<small></small><span></span>";
        b.onclick = () => { selectedId = seg.id; renderedId = null; render(job); };
        row.append(c,b); panel.append(row);
      }
      const draftChanged = seg.accepted && ["text","reading","control","pause_before_sec","convert_numbers","number_reading_style"].some(key=>(seg.draft[key] ?? null) !== (accepted(seg)?.[key] ?? null));
      const review = draftChanged || seg.versions.some(v => v.id !== seg.accepted && !seg.history.includes(v.id)) || accepted(seg)?.evaluation?.awkward || !!accepted(seg)?.content_check?.warnings?.length || !!seg.error;
      row.hidden = filter === "pending" ? !!seg.accepted : filter === "ready" ? !seg.accepted : filter === "review" ? !review : false;
      row.classList.toggle("active",seg.id === selectedId);
      row.querySelector("input").checked = checked.has(seg.id);
      row.querySelector("small").textContent = `${seg.id} · ${seg.status === "running" ? "生成中" : seg.accepted ? "採用済み" : "未生成"}${draftChanged ? " · 編集未反映" : review ? " · 要確認" : ""}`;
      row.querySelector("span").textContent = seg.draft.text;
    }
    for(const row of [...panel.children]) if(!job.segments.some(s => s.id === row.dataset.id)) row.remove();
  }
  let versionsSignature = "";
  function renderVersions(seg) {
    const signature = JSON.stringify([job.id,seg.id,seg.versions,seg.accepted,busy()]);
    if(signature === versionsSignature) return;
    versionsSignature = signature;
    $("versions").replaceChildren(...[...seg.versions].reverse().map((v) => {
      const row = document.createElement("div"); row.className = "version-row";
      const label = document.createElement("span"); const isAccepted = seg.accepted === v.id;
      label.className = isAccepted ? "adopted" : "";
      const score = v.evaluation ? ` · 評価 ${v.evaluation.overall.toFixed(2)}${v.evaluation.awkward ? " 要確認" : ""}${v.evaluation.llm_reason && v.evaluation.llm_score == null ? "（LLM 未評価）" : ""}` : "";
      const gate = v.content_check;
      const gateText = gate ? (gate.passed ? (gate.warnings?.length ? " · 音声検査 合格・確認事項あり" : " · 音声検査 合格") : " · 音声検査 未合格") : " · 音声検査 未実施";
      label.textContent = `${isAccepted ? "採用中" : seg.history.includes(v.id) ? "以前の音声" : "候補"} · ${v.duration_sec.toFixed(1)}秒${score}${gateText}`;
      label.title = `${v.text}\n話し方: ${v.control}\n間: ${v.pause_before_sec}秒\n生成日時: ${v.created_at}`;
      const play = document.createElement("button"); play.textContent = "試聴"; play.onclick = () => playOne(seg.id,v.id);
      const adopt = document.createElement("button"); adopt.textContent = isAccepted ? "採用済み" : seg.history.includes(v.id) ? "元に戻す" : "採用";
      adopt.disabled = (isAccepted && !!gate?.passed) || busy() || (!!gate && !gate.passed && !gate.unavailable);
      adopt.onclick = () => action(async () => {
        if(readLocal(draftKey(job.id,seg.id))) throw new Error("未保存の編集があります。先に編集を保存してください。");
        const data = await post(`/api/jobs/${job.id}/segments/${seg.id}/adopt`, {...request(),expected_revision:seg.revision,version_id:v.id}); render(data);
      });
      const details=document.createElement("small");details.className="version-details";
      details.textContent=`${v.text} — 間 ${v.pause_before_sec}秒 / ${v.control || "話し方指定なし"}`;
      if (v.prepared_reading) details.textContent += ` — 生成に使用した読み: ${v.prepared_reading}`;
      if (v.speech_rate) details.textContent += ` — 話速: ${v.speech_rate.reason}（目標 ${v.speech_rate.target.toFixed(1)} モーラ/秒）`;
      if (v.speech_rate?.applied) details.textContent += ` / ${v.speech_rate.before.toFixed(1)} → ${v.speech_rate.after.toFixed(1)} モーラ/秒（${v.speech_rate.factor.toFixed(2)}倍）`;
      if (v.speech_rate?.estimated_terms?.length) details.textContent += `（読み推定: ${v.speech_rate.estimated_terms.join("、")}）`;
      if (gate && !gate.passed) details.textContent += " — " + gate.reasons.join(" / ");
      if (gate?.warnings?.length) details.textContent += " — " + gate.warnings.join(" / ");
      const recheck = document.createElement("button"); recheck.textContent = "音声を再検査";
      recheck.disabled = !!busy();
      recheck.onclick = () => action(async () => render(await post(`/api/jobs/${job.id}/segments/${seg.id}/recheck`, {...request(),expected_revision:seg.revision,version_id:v.id})));
      row.append(label,play,adopt,recheck,details); return row;
    }));
  }
  function render(data) {
    if(job && job.id !== data.id) return;
    job = data;
    $("job-title").textContent = job.title; $("job-status").textContent = labels[job.status] || job.status;
    const regen = job.operation === "regenerate-all" ? job.regeneration_progress : null;
    const percent = regen ? Math.round(100 * regen.current / Math.max(1,regen.total)) : job.percent;
    $("progress-fill").style.width = `${percent}%`; $("progress").setAttribute("aria-valuenow",percent);
    $("progress-text").textContent = regen ? `全て再生成 ${regen.current}/${regen.total} · ${job.error || job.message}` : `${job.current}/${job.total} 採用済み · ${job.error || job.message}`;
    const times = job.timings.slice(-5).map(t=>t.generation_sec);
    const seconds = times.length ? times.reduce((a,b)=>a+b,0)/times.length * (job.total-job.current) : null;
    $("timing").textContent = busy() && job.operation === "generate" && seconds > 0 ? `残り目安 ${Math.ceil(seconds/60)}分（生成実測から推定）` : "";
    $("cancel").classList.toggle("hidden",!busy()); $("cancel").disabled = job.cancel_requested;
    for(const id of ["regenerate-all","generate-all","generate-selected","regenerate","save-edit","judge","save-dictionary","find-pronunciations","import-pronunciations"]) $(id).disabled = !!busy();
    for(const button of $("pronunciation-candidates").querySelectorAll("button")) button.disabled = !!busy();
    const readingPreview = job.pronunciation_preview;
    $("play-pronunciation").classList.toggle("hidden", !readingPreview);
    $("pronunciation-preview-note").textContent = readingPreview ? `試聴時の読み：${readingPreview.term} → ${readingPreview.reading}（${readingPreview.segment_id}）` : "";
    $("play-all").disabled = job.current === 0;
    renderSegments();
    const seg = current();
    if(seg) {
      $("segment-title").textContent = `${seg.id} の編集`;
      $("original-text").textContent = `元の本文：${seg.original_text}`;
      const editing = ["edit-text","edit-reading","edit-control","edit-pause","edit-convert-numbers","edit-number-reading-style"].includes(document.activeElement?.id);
      if(renderedId !== `${job.id}/${seg.id}/${seg.revision}` && !editing) fillEditor(seg);
      const local=readLocal(draftKey(job.id,seg.id));
      $("resolve-edit").classList.toggle("hidden",!local || local.revision===seg.revision || (local.base && JSON.stringify(local.base)===JSON.stringify(seg.draft)));
      renderVersions(seg);
      $("judge").disabled = busy() || !seg.accepted || !audioJudgeModel();
      const feedback = seg.feedback?.version_id === seg.accepted ? seg.feedback : null;
      $("feedback-text").textContent = feedback?.feedback || "";
      $("suggestion").classList.toggle("hidden",!feedback);
      if(feedback) { $("suggested-text").value = feedback.revised_text; $("suggested-control").textContent = feedback.voice_design_prompt; }
    }
    $("exports").classList.toggle("hidden",!job.export);
    $("export-note").textContent = job.export ? `採用済みの音声を結合 · ${(job.duration_sec/60).toFixed(1)}分。未採用の候補や編集内容は含みません。` : "すべてのセグメントを生成・採用すると書き出せます。";
    $("download").href = `/api/jobs/${job.id}/download`; $("download-normalized").href = `/api/jobs/${job.id}/download?normalize=true`; $("archive").href = `/api/jobs/${job.id}/archive`;
    $("archive-finished").href = `/api/jobs/${job.id}/archive?normalize=true`;
  }
  async function poll() {
    if(!job || polling) return;
    const id = job.id; polling = true;
    try { const data = await api(`/api/jobs/${id}`); if(job?.id === id) render(data); await refreshLibrary(); }
    catch(err) { notice(`接続を確認してください。入力内容は保持されています。${err.message}`,true); }
    finally { polling = false; }
  }
  setInterval(poll,1500);
  $("filter").onchange = () => {if(job) renderSegments();};
  let setupRevision = 0;
  $("new-job").onclick = () => {
    closeRename(); job=null;selectedId=null;renderedId=null;sequence=[];
    updateProviderUI();
    setupRevision++;
    $("title").value="";$("script").value="";$("script-file").value="";$("preview-list").replaceChildren();
    saveSetup();
    $("audio").pause();$("setup").classList.remove("hidden");$("studio").classList.add("hidden");notice("");history.replaceState(null,"","/");refreshLibrary().catch(err=>notice(err.message,true));
  };
  $("script-file").onchange = async () => {
    const file = $("script-file").files[0]; if(!file) return;
    if(file.size > 1000000) return notice("台本ファイルは1 MB以内にしてください",true);
    const revision=setupRevision, text=await file.text();
    if(revision !== setupRevision || $("script-file").files[0] !== file)return;
    $("script").value = text;
    $("mode").value = /\.(ssml|xml)$/i.test(file.name) ? "ssml" : /\.(md|markdown)$/i.test(file.name) ? "markdown" : "plain";
    saveSetup();
  };
  function saveSetup() { store("voxcpm.setup",{title:$("title").value,script:$("script").value,mode:$("mode").value}); }
  for(const id of ["title","script","mode"]) $(id).addEventListener("input",saveSetup);
  $("preview").onclick = () => action(async () => {
    const revision=setupRevision;
    const data = await post("/api/preview",{script:$("script").value,mode:$("mode").value,config:config()});
    if(revision !== setupRevision)return;
    $("preview-list").replaceChildren(...data.segments.map(seg => { const li=document.createElement("li");li.textContent=`${seg.id} · ${seg.text}（直前の間 ${seg.pause_before_sec}秒） — 読み: ${seg.prepared_reading}`;return li; }));
    notice(`${data.segments.length} セグメントに分割されます`);
  });
  $("job-form").onsubmit = (event) => {event.preventDefault();action(async () => {
    const body=new FormData();body.append("script",$("script").value);body.append("mode",$("mode").value);body.append("title",$("title").value);body.append("config",JSON.stringify(config()));
    window.referenceRecorder.appendTo(body);
    const data=await api("/api/jobs",{method:"POST",body});await openJob(data.id);
    notice(data.reference_warnings.join("\n") || "制作を保存しました。まず1〜2箇所を選んで試聴できます。");
  });};
  $("save-edit").onclick = () => action(saveEdit);
  $("resolve-edit").onclick = () => action(async()=>{
    const seg=current();if(busy())throw new Error("処理が終わってから適用してください");
    store(draftKey(job.id,seg.id),{draft:editValue(),revision:seg.revision,base:seg.draft});
    await saveEdit();
  });
  async function generate(ids) {
    if(job.config.improve_llm && !$("api-key").value && !providerKeyConfigured()) throw new Error("選択したプロバイダーの API キーを入力してください");
    await saveEdit();
    const pendingLocal = job.segments.some(s => (ids ? ids.includes(s.id) : !s.accepted) && readLocal(draftKey(job.id,s.id)));
    if(pendingLocal) throw new Error("選択箇所に未保存の編集があります。各箇所で編集を保存してください。");
    render(await post(`/api/jobs/${job.id}/generate`,{...request(),segment_ids:ids}));
  }
  $("regenerate-all").onclick = () => action(async () => {
    await saveEdit();
    if (job.segments.some(s => readLocal(draftKey(job.id,s.id)))) throw new Error("未保存の編集を各箇所で保存してください。");
    requireSavedDictionary();
    if (!confirm(`「${job.title}」の全${job.segments.length}箇所を再生成します。全て検査に合格したら一括採用します。以前の音声は履歴に残ります。実行しますか？`)) return;
    render(await post(`/api/jobs/${job.id}/regenerate-all`, request()));
  });
  $("generate-all").onclick = () => action(()=>generate(null));
  $("generate-selected").onclick = () => action(async()=>{if(!checked.size) throw new Error("一覧のチェックボックスで試聴箇所を選択してください");await generate([...checked]);});
  $("cancel").onclick = () => action(async()=>render(await post(`/api/jobs/${job.id}/cancel`)));
  $("regenerate").onclick = () => action(async()=>{await saveEdit();const seg=current();render(await post(`/api/jobs/${job.id}/segments/${seg.id}/regenerate`,{...request(),expected_revision:seg.revision}));});
  $("judge").onclick = () => action(async()=>{const seg=current();render(await post(`/api/jobs/${job.id}/segments/${seg.id}/audio-judge`,{...request(),expected_revision:seg.revision,model:audioJudgeModel()}));});
  $("use-suggestion").onclick = () => { const feedback=current()?.feedback;if(!feedback)return;$("edit-text").value=feedback.revised_text;$("edit-control").value=feedback.voice_design_prompt;trackEdit();notice("提案を編集欄に取り込みました。元の本文と比べ、意味や固有名詞を確認してください。"); };
  $("save-dictionary").onclick = () => action(async()=>{
    const entries={};for(const line of $("dictionary").value.split("\n").filter(l=>l.trim())) {const index=line.indexOf("=");if(index<1)throw new Error("読み辞書は「表記=読み」の形式で入力してください");entries[line.slice(0,index).trim()]=line.slice(index+1).trim();}
    const data=await api(`/api/jobs/${job.id}/dictionary`,{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify({entries})});
    $("dictionary").value=dictionaryText(data.dictionary);render(data);await loadPronunciations();notice("読み辞書を保存しました。次の生成から適用されます。");
  });
  function requireSavedDictionary() {
    if ($("dictionary").value !== dictionaryText(job.dictionary)) throw new Error("直接編集した読み辞書を先に保存してください。");
  }
  async function loadPronunciations() {
    const jid=job.id, data=await post(`/api/jobs/${jid}/pronunciation-candidates`,{include_registered:$("show-registered-readings").checked});
    if(job?.id !== jid)return;
    lexiconRevision=data.lexicon.revision;
    $("lexicon-count").textContent=`共通辞書 ${Object.keys(data.lexicon.entries).length}語 · この制作 ${Object.keys(job.dictionary).length}語`;
    const cards=data.candidates.map(candidate=>{
      const card=document.createElement("div");card.className="reading-card";card.dataset.category=candidate.category || "other";
      const title=document.createElement("strong");title.textContent=`${candidate.term} · ${candidate.occurrences}箇所${candidate.status === "known" ? " · 登録済み" : ""}`;
      const context=document.createElement("p");context.className="muted small";context.textContent=candidate.context;
      const key=`voxcpm.reading.${jid}.${candidate.term}`, saved=readLocal(key);
      const label=document.createElement("label");label.textContent=`${candidate.term} の読み（かな）`;
      const input=document.createElement("input");input.maxLength=200;input.value=saved?.reading ?? candidate.reading;input.placeholder="ひらがな・カタカナで入力";label.append(input);
      const shareLabel=document.createElement("label");shareLabel.className="check";
      const share=document.createElement("input");share.type="checkbox";share.checked=saved?.shared ?? false;
      shareLabel.append(share,document.createTextNode("次の制作でも使う（共通辞書へ登録）"));
      const saveDraft=()=>store(key,{reading:input.value,shared:share.checked});
      input.oninput=saveDraft;share.onchange=saveDraft;
      const buttons=document.createElement("div");buttons.className="actions";
      const preview=document.createElement("button");preview.textContent="この読みを文中で試聴生成";
      preview.onclick=()=>action(async()=>{
        if(job?.id!==jid)return;requireSavedDictionary();await saveEdit();
        if(job.segments.some(s=>readLocal(draftKey(jid,s.id))))throw new Error("本文の編集を保存してから試聴してください。");
        render(await post(`/api/jobs/${jid}/pronunciation-preview`,{
          request_id:crypto.randomUUID(),term:candidate.term,reading:input.value.trim(),
          segment_id:candidate.segment_id,expected_revision:candidate.segment_revision
        }));notice("試聴音声を生成しています。完了後に「最新の読み試聴を再生」で確認できます。");
      });
      const save=document.createElement("button");save.textContent="読みを登録";
      save.onclick=()=>action(async()=>{
        if(job?.id!==jid)return;requireSavedDictionary();
        const data=await post(`/api/jobs/${jid}/pronunciation-learn`,{term:candidate.term,
          reading:input.value.trim(),shared:share.checked,expected_revision:lexiconRevision});
        localStorage.removeItem(key);
        if(job?.id!==jid)return;
        $("dictionary").value=dictionaryText(data.dictionary);render(data);await loadPronunciations();
        notice("読みを登録しました。次の生成から適用されます。");
      });
      buttons.append(preview,save);card.append(title,context,label,shareLabel,buttons);return card;
    });
    if(!cards.length){const message=document.createElement("p");message.textContent="未登録の確認候補はありません。必要な読みは辞書の直接編集でも登録できます。";cards.push(message);}
    $("pronunciation-candidates").replaceChildren(...cards);
    filterPronunciations();
    for(const button of $("pronunciation-candidates").querySelectorAll("button"))button.disabled=!!busy();
  }
  $("find-pronunciations").onclick=()=>action(async()=>{
    await saveEdit();
    if(job.segments.some(s=>readLocal(draftKey(job.id,s.id))))throw new Error("他の箇所の本文編集も保存してから候補を更新してください。");
    await loadPronunciations();notice("保存済みの最新本文から候補を更新しました。");
  });
  $("show-registered-readings").onchange=()=>action(loadPronunciations);
  function filterPronunciations() {
    const filter=$("reading-filter").value, cards=[...$("pronunciation-candidates").querySelectorAll(".reading-card")];
    for(const card of cards) card.hidden=filter !== "all" && card.dataset.category !== filter;
    const count=cards.filter(card=>!card.hidden).length;
    $("reading-filter-status").textContent=`${count} / ${cards.length} 語を表示${!count && cards.length ? " · この条件に一致する候補はありません" : ""}`;
  }
  $("reading-filter").onchange=filterPronunciations;
  $("import-pronunciations").onclick=()=>action(async()=>{
    requireSavedDictionary();const jid=job.id,data=await post(`/api/jobs/${jid}/pronunciation-import`);
    if(job?.id!==jid)return;$("dictionary").value=dictionaryText(data.dictionary);render(data);
    await loadPronunciations();notice("共通辞書を取り込みました。この制作で登録した読みは優先されます。");
  });
  $("play-pronunciation").onclick=()=>{
    const preview=job?.pronunciation_preview;if(!preview)return;
    sequence=[];$("audio").src=`/api/jobs/${job.id}/pronunciation-preview/${preview.id}`;
    $("player-label").textContent=`読み確認：${preview.term} → ${preview.reading}`;
    $("player-detail").textContent=preview.text;$("audio").play().catch(err=>notice(err.message,true));
  };
  $("fork-script").onclick = () => {const text=job.script, title=job.title,mode=job.mode;$("new-job").click();$("script").value=text;$("title").value=title+"（改訂）";$("mode").value=mode;saveSetup();notice("台本全体の改訂は新しい制作として保存します。元の制作と音声は保持されます。参照音声は再選択してください。");window.referenceRecorder.clear();};
  function playOne(sid,vid,continuous=false) {
    if(!continuous) sequence=[];
    const seg=job.segments.find(s=>s.id===sid),v=seg.versions.find(v=>v.id===vid);
    $("audio").src=audioUrl(sid,vid);$("player-label").textContent=`${sid} · ${seg.accepted===vid?"採用中":"候補"}`;$("player-detail").textContent=v.text;
    for(const row of $("segments").children)row.classList.toggle("playing",row.dataset.id===sid);
    $("audio").play().catch(err=>notice(`再生できませんでした: ${err.message}`,true));
  }
  $("play-all").onclick = () => {sequence=job.segments.filter(s=>s.accepted).map(s=>[s.id,s.accepted]);const next=sequence.shift();if(next)playOne(...next,true);};
  $("audio").onended = () => {const next=sequence.shift();if(next)playOne(...next,true);else for(const row of $("segments").children)row.classList.remove("playing");};
  $("stop").onclick = () => {sequence=[];$("audio").pause();$("audio").currentTime=0;};
  $("play-full").onclick = () => {sequence=[];$("audio").src=`/api/jobs/${job.id}/download?v=${job.export.id}`;$("player-label").textContent="全体音声";$("player-detail").textContent=job.title;$("audio").play().catch(err=>notice(err.message,true));};
  let finishedAudioUrl = null;
  async function finishedBlob(url) {
    notice("音量と文間を調整しています。初回は少し時間がかかります。");
    const response = await fetch(url);
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.detail || "仕上げに失敗しました。原音は保持されています。");
    }
    return URL.createObjectURL(await response.blob());
  }
  $("play-finished").onclick = () => action(async () => {
    const jid = job.id, exportId = job.export.id, title = job.title;
    const url = await finishedBlob(`/api/jobs/${jid}/download?normalize=true`);
    if (job.id !== jid || job.export?.id !== exportId) {URL.revokeObjectURL(url);return;}
    if (finishedAudioUrl) URL.revokeObjectURL(finishedAudioUrl);
    finishedAudioUrl = url; sequence=[]; $("audio").src=url;
    $("player-label").textContent="仕上げ後の全体音声";$("player-detail").textContent=title;
    await $("audio").play();notice("仕上げ後の音声を再生します");
  });
  for (const [id, suffix] of [["download-normalized", "wav"], ["archive-finished", "zip"]]) {
    $(id).onclick = event => {event.preventDefault();const url=$(id).href,jid=job.id;action(async () => {
      const objectUrl = await finishedBlob(url), link=document.createElement("a");
      link.href=objectUrl;link.download=`narration_${jid}_finished.${suffix}`;
      document.body.append(link);link.click();link.remove();
      setTimeout(()=>URL.revokeObjectURL(objectUrl),60000);notice("仕上げ音声を書き出しました");
    });};
  }
  $("clear-key").onclick = () => {$("api-key").value="";localStorage.removeItem("voxcpm.openrouter.api_key");notice("入力キーと旧 localStorage キーを消去しました");};
  const providerKeyConfigured = () => provider() === "azure" ? settings.azure_api_key_configured : settings.openrouter_api_key_configured;
  function updateProviderUI() {
    const azure=provider() === "azure";
    $("llm-provider").disabled=!!job;
    $("provider-key-label").firstChild.textContent=azure ? "Azure OpenAI API キー" : "OpenRouter API キー";
    $("openrouter-model-field").classList.toggle("hidden",azure);
    $("azure-fields").classList.toggle("hidden",!azure);
    $("azure-text-deployment").disabled=!!job;
    $("refresh-models").classList.toggle("hidden",azure);
    $("key-status").textContent=azure
      ? `${settings.azure_endpoint_configured ? "Azure エンドポイント設定済み" : "サーバーに AZURE_OPENAI_ENDPOINT を設定してください"} · ${providerKeyConfigured() ? "サーバーの API キーを利用できます" : "API キーを入力してください"}`
      : providerKeyConfigured() ? "サーバーの API キーを利用できます" : "外部評価を使う場合のみキーが必要です";
    $("feedback-provider-note").textContent=`採用中の音声と本文を${azure ? "Azure OpenAI Service" : "OpenRouter"}に送信します。提案は自動で採用されません（制作あたり最大50回）。`;
  }
  $("llm-provider").onchange=()=>{$("api-key").value="";updateProviderUI();};
  $("azure-audio-deployment").oninput=()=>{if(job)$("judge").disabled=busy() || !current()?.accepted || !audioJudgeModel();};
  function fillModels(models) {
    const previous=$("llm-model").value, audioModels=models.filter(m=>m.supportsAudio === true);
    $("llm-model").replaceChildren(...audioModels.map(m=>{const o=document.createElement("option");o.value=m.id;o.textContent=`${m.name || m.id} · 音声入力対応`;return o;}));
    if(audioModels.some(m=>m.id===previous))$("llm-model").value=previous;
    else if(audioModels.some(m=>m.id===settings.default_audio_model))$("llm-model").value=settings.default_audio_model;
    if(!audioModels.length){const o=document.createElement("option");o.value="";o.textContent="音声入力対応モデルがありません";$("llm-model").append(o);}
    $("llm-model").disabled=!audioModels.length;
    $("judge").disabled=!!busy() || !current()?.accepted || !audioJudgeModel();
  }
  $("refresh-models").onclick = () => action(async()=>{const data=await api("/api/llm/models",{headers:{"X-API-Key":$("api-key").value.trim()}});fillModels(data.models);notice(`${data.models.length} モデルを取得しました`);});
  $("import-run").onclick = () => action(async()=>{if(!$("legacy-runs").value)throw new Error("取り込める run がありません");const data=await post("/api/import",{run:$("legacy-runs").value});await openJob(data.id);notice("元のファイルを保持して取り込みました");});
  document.addEventListener("keydown",event=>{if((event.ctrlKey||event.metaKey)&&event.key==="s"&&job){event.preventDefault();action(saveEdit);}});
  async function init() {
    const saved=readLocal("voxcpm.setup");if(saved){$("title").value=saved.title;$("script").value=saved.script;$("mode").value=saved.mode;}
    settings=await api("/api/llm/settings");fillModels(settings.suggested_models);
    $("llm-provider").value=settings.default_provider || "openrouter";
    $("azure-text-deployment").value=settings.azure_text_deployment || "";
    $("azure-audio-deployment").value=settings.azure_audio_deployment || "";
    updateProviderUI();
    if(localStorage.getItem("voxcpm.openrouter.api_key"))notice("以前保存された API キーがあります。外部評価設定の「旧保存キーを消去」で削除できます。");
    const health=await api("/api/health");$("health").textContent=health.ok?"接続中":"接続エラー";
    await refreshLibrary();const legacy=await api("/api/legacy-runs");$("legacy-runs").replaceChildren(...legacy.runs.map(run=>{const o=document.createElement("option");o.value=run;o.textContent=run;return o;}));
    const id=new URLSearchParams(location.search).get("job");if(id)await openJob(id);
  }
  init().catch(err=>notice(err.message,true));
})();
