(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const activeStates = new Set(["queued", "running", "improving"]);
  const labels = {draft:"下書き",queued:"待機中",running:"処理中",improving:"評価中",done:"保存済み",interrupted:"中断",error:"エラー"};
  let job = null, selectedId = null, renderedId = null, polling = false, sequence = [], librarySignature = "";
  let settings = {}, previewUrl = null, requestBusy = false;
  let lexiconRevision = 0;
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
  const config = () => ({device:$("device").value,control:$("control").value,max_chars:+$("max-chars").value,
    cfg_value:+$("cfg").value,timesteps:+$("timesteps").value,seed:+$("seed").value,
    improve:$("improve").checked,improve_asr:$("asr").checked,improve_llm:$("llm").checked,
    improve_rounds:+$("rounds").value,llm_model:$("llm-model").value || settings.default_model});

  async function refreshLibrary() {
    const data = await api("/api/jobs");
    const signature = JSON.stringify(data.jobs) + (job?.id || "");
    if(signature === librarySignature) return;
    librarySignature = signature;
    $("library").replaceChildren(...data.jobs.map(j => {
      const b = document.createElement("button"); b.className = "library-item" + (job?.id === j.id ? " active" : "");
      const title = document.createElement("span"); title.textContent = j.title;
      const meta = document.createElement("small"); meta.textContent = `${labels[j.status] || j.status} · ${j.ready}/${j.total}`;
      b.append(title,meta); b.onclick = () => action(() => openJob(j.id)); return b;
    }));
  }
  async function openJob(id) {
    const data = await api(`/api/jobs/${encodeURIComponent(id)}`);
    sequence = []; $("audio").pause(); checked.clear(); selectedId = data.segments[0]?.id; renderedId = null;
    job = data; history.replaceState(null,"",`?job=${id}`);
    $("setup").classList.add("hidden"); $("studio").classList.remove("hidden");
    $("dictionary").value = Object.entries(data.dictionary).map(([k,v]) => `${k}=${v}`).join("\n");
    $("pronunciation-candidates").replaceChildren();
    render(data); await refreshLibrary(); await loadPronunciations();
  }
  function editValue() {
    return {text:$("edit-text").value,reading:$("edit-reading").value,control:$("edit-control").value,pause_before_sec:+$("edit-pause").value};
  }
  function trackEdit() {
    const seg = current(); if(!seg) return;
    const prior = readLocal(draftKey(job.id,seg.id));
    store(draftKey(job.id,seg.id), {draft:editValue(),revision:prior?.revision ?? seg.revision,base:prior?.base ?? seg.draft});
    $("save-status").textContent = "未保存（このブラウザに一時保存）";
  }
  ["edit-text","edit-reading","edit-control","edit-pause"].forEach(id => $(id).addEventListener("input", trackEdit));
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
      const draftChanged = seg.accepted && ["text","reading","control","pause_before_sec"].some(key=>seg.draft[key] !== accepted(seg)?.[key]);
      const review = draftChanged || seg.versions.some(v => v.id !== seg.accepted && !seg.history.includes(v.id)) || accepted(seg)?.evaluation?.awkward || !!seg.error;
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
      const gateText = gate ? (gate.passed ? " · 音声検査 合格" : " · 音声検査 未合格") : " · 音声検査 未実施";
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
      if (gate && !gate.passed) details.textContent += " — " + gate.reasons.join(" / ");
      row.append(label,play,adopt,details); return row;
    }));
  }
  function render(data) {
    if(job && job.id !== data.id) return;
    job = data;
    $("job-title").textContent = job.title; $("job-status").textContent = labels[job.status] || job.status;
    $("progress-fill").style.width = `${job.percent}%`; $("progress").setAttribute("aria-valuenow",job.percent);
    $("progress-text").textContent = `${job.current}/${job.total} 採用済み · ${job.error || job.message}`;
    const times = job.timings.slice(-5).map(t=>t.generation_sec);
    const seconds = times.length ? times.reduce((a,b)=>a+b,0)/times.length * (job.total-job.current) : null;
    $("timing").textContent = busy() && job.operation === "generate" && seconds > 0 ? `残り目安 ${Math.ceil(seconds/60)}分（生成実測から推定）` : "";
    $("cancel").classList.toggle("hidden",!busy()); $("cancel").disabled = job.cancel_requested;
    for(const id of ["generate-all","generate-selected","regenerate","save-edit","judge","save-dictionary","find-pronunciations","import-pronunciations"]) $(id).disabled = !!busy();
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
      const editing = ["edit-text","edit-reading","edit-control","edit-pause"].includes(document.activeElement?.id);
      if(renderedId !== `${job.id}/${seg.id}/${seg.revision}` && !editing) fillEditor(seg);
      const local=readLocal(draftKey(job.id,seg.id));
      $("resolve-edit").classList.toggle("hidden",!local || local.revision===seg.revision || (local.base && JSON.stringify(local.base)===JSON.stringify(seg.draft)));
      renderVersions(seg);
      $("judge").disabled = busy() || !seg.accepted;
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
  $("new-job").onclick = () => { job=null;selectedId=null;renderedId=null;sequence=[];$("audio").pause();$("setup").classList.remove("hidden");$("studio").classList.add("hidden");notice("");history.replaceState(null,"","/");refreshLibrary().catch(err=>notice(err.message,true)); };
  $("script-file").onchange = async () => {
    const file = $("script-file").files[0]; if(!file) return;
    if(file.size > 1000000) return notice("台本ファイルは1 MB以内にしてください",true);
    $("script").value = await file.text();
    $("mode").value = /\.(ssml|xml)$/i.test(file.name) ? "ssml" : /\.(md|markdown)$/i.test(file.name) ? "markdown" : "plain";
    saveSetup();
  };
  $("reference").onchange = () => {
    if(previewUrl) URL.revokeObjectURL(previewUrl);
    const file = $("reference").files[0]; $("reference-player").classList.toggle("hidden",!file);
    if(file) { previewUrl=URL.createObjectURL(file);$("reference-player").src=previewUrl; }
  };
  function saveSetup() { store("voxcpm.setup",{title:$("title").value,script:$("script").value,mode:$("mode").value}); }
  for(const id of ["title","script","mode"]) $(id).addEventListener("input",saveSetup);
  $("preview").onclick = () => action(async () => {
    const data = await post("/api/preview",{script:$("script").value,mode:$("mode").value,config:config()});
    $("preview-list").replaceChildren(...data.segments.map(seg => { const li=document.createElement("li");li.textContent=`${seg.id} · ${seg.text}（直前の間 ${seg.pause_before_sec}秒）`;return li; }));
    notice(`${data.segments.length} セグメントに分割されます`);
  });
  $("job-form").onsubmit = (event) => {event.preventDefault();action(async () => {
    const body=new FormData();body.append("script",$("script").value);body.append("mode",$("mode").value);body.append("title",$("title").value);body.append("config",JSON.stringify(config()));
    if($("reference").files[0]) body.append("reference",$("reference").files[0]);
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
    if(job.config.improve_llm && !$("api-key").value && !settings.api_key_configured) throw new Error("外部評価の API キーを入力してください");
    await saveEdit();
    const pendingLocal = job.segments.some(s => (ids ? ids.includes(s.id) : !s.accepted) && readLocal(draftKey(job.id,s.id)));
    if(pendingLocal) throw new Error("選択箇所に未保存の編集があります。各箇所で編集を保存してください。");
    render(await post(`/api/jobs/${job.id}/generate`,{...request(),segment_ids:ids}));
  }
  $("generate-all").onclick = () => action(()=>generate(null));
  $("generate-selected").onclick = () => action(async()=>{if(!checked.size) throw new Error("一覧のチェックボックスで試聴箇所を選択してください");await generate([...checked]);});
  $("cancel").onclick = () => action(async()=>render(await post(`/api/jobs/${job.id}/cancel`)));
  $("regenerate").onclick = () => action(async()=>{await saveEdit();const seg=current();render(await post(`/api/jobs/${job.id}/segments/${seg.id}/regenerate`,{...request(),expected_revision:seg.revision}));});
  $("judge").onclick = () => action(async()=>{const seg=current();render(await post(`/api/jobs/${job.id}/segments/${seg.id}/audio-judge`,{...request(),expected_revision:seg.revision,model:$("llm-model").value}));});
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
      const card=document.createElement("div");card.className="reading-card";
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
    for(const button of $("pronunciation-candidates").querySelectorAll("button"))button.disabled=!!busy();
  }
  $("find-pronunciations").onclick=()=>action(async()=>{
    await saveEdit();
    if(job.segments.some(s=>readLocal(draftKey(job.id,s.id))))throw new Error("他の箇所の本文編集も保存してから候補を更新してください。");
    await loadPronunciations();notice("保存済みの最新本文から候補を更新しました。");
  });
  $("show-registered-readings").onchange=()=>action(loadPronunciations);
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
  $("fork-script").onclick = () => {const text=job.script, title=job.title,mode=job.mode;$("new-job").click();$("script").value=text;$("title").value=title+"（改訂）";$("mode").value=mode;saveSetup();notice("台本全体の改訂は新しい制作として保存します。元の制作と音声は保持されます。参照音声は再選択してください。");$("reference").value="";$("reference-player").classList.add("hidden");};
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
  function fillModels(models) {const previous=$("llm-model").value;$("llm-model").replaceChildren(...models.map(m=>{const o=document.createElement("option");o.value=m.id;o.textContent=`${m.name || m.id}${m.supportsAudio?" · 音声入力対応":""}`;return o;}));if(models.some(m=>m.id===previous))$("llm-model").value=previous;}
  $("refresh-models").onclick = () => action(async()=>{const data=await api("/api/llm/models",{headers:{"X-API-Key":$("api-key").value.trim()}});fillModels(data.models);notice(`${data.models.length} モデルを取得しました`);});
  $("import-run").onclick = () => action(async()=>{if(!$("legacy-runs").value)throw new Error("取り込める run がありません");const data=await post("/api/import",{run:$("legacy-runs").value});await openJob(data.id);notice("元のファイルを保持して取り込みました");});
  document.addEventListener("keydown",event=>{if((event.ctrlKey||event.metaKey)&&event.key==="s"&&job){event.preventDefault();action(saveEdit);}});
  async function init() {
    const saved=readLocal("voxcpm.setup");if(saved){$("title").value=saved.title;$("script").value=saved.script;$("mode").value=saved.mode;}
    settings=await api("/api/llm/settings");fillModels(settings.suggested_models);$("llm-model").value=settings.default_audio_model;
    $("key-status").textContent=settings.api_key_configured?"サーバーの API キーを利用できます":"外部評価を使う場合のみキーが必要です";
    if(localStorage.getItem("voxcpm.openrouter.api_key"))notice("以前保存された API キーがあります。外部評価設定の「旧保存キーを消去」で削除できます。");
    const health=await api("/api/health");$("health").textContent=health.ok?"接続中":"接続エラー";
    await refreshLibrary();const legacy=await api("/api/legacy-runs");$("legacy-runs").replaceChildren(...legacy.runs.map(run=>{const o=document.createElement("option");o.value=run;o.textContent=run;return o;}));
    const id=new URLSearchParams(location.search).get("job");if(id)await openJob(id);
  }
  init().catch(err=>notice(err.message,true));
})();
