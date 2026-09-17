(() => {
  "use strict";
  const $ = id => document.getElementById(id);
  const examples = {
    standard: ["日本語、明瞭な声、自然な抑揚、会話に近いテンポ", "今日は、新しい取り組みについてご紹介します。まず、全体の流れを確認しましょう。身近な例を交えながら、大切なポイントを一つずつお伝えします。", "相手に説明するつもりで、普段の会話に近い速さで読みましょう。"],
    calm: ["日本語、落ち着いた声、穏やかな抑揚、ゆったりしたテンポ", "朝の光が、窓から静かに差し込みます。いつもの景色にも、少し目を向けるだけで、新しい発見があります。今日は、その小さな変化をたどってみましょう。", "文の終わりを急がず、穏やかに息をつないでください。"],
    bright: ["日本語、明るく親しみやすい声、軽やかな抑揚、軽快なテンポ", "みなさん、こんにちは。今日は、毎日が少し便利になる新しいサービスをご紹介します。使い方はとても簡単です。それでは、さっそく一緒に見ていきましょう。", "笑顔で案内するように、言葉をはっきり伝えましょう。"],
    presentation: ["日本語、明瞭で落ち着いた声、要点を強調する抑揚、聞き取りやすいテンポ", "本日は、業務をより進めやすくするための提案をお話しします。大切な点は、二つあります。一つは、情報を共有すること。もう一つは、小さく試して改善することです。", "「二つ」「一つ」「もう一つ」を意識し、要点の前で少し間を取りましょう。"],
    gentle: ["日本語、やさしく温かい声、柔らかな抑揚、急がないテンポ", "初めてのことには、少し戸惑うかもしれません。大丈夫です。自分のペースで、一つずつ進めていきましょう。わからないところは、何度でも確かめてください。", "一人の相手に寄り添うように、声を張りすぎず読んでください。"]
  };
  let selectedFile = null, transcript = "", recordedStyle = "", url = null;
  let recorder = null, stream = null, timer = null, pending = false, recording = false, token = 0;
  const supported = !!(navigator.mediaDevices?.getUserMedia && window.MediaRecorder);
  function status(text) { $("record-status").textContent = text; }
  function controls() {
    for (const id of ["voice-style", "control", "reference-script", "reference", "reference-clear", "reference-transcript", "transcript-confirmed", "use-transcript", "transcribe-reference"]) $(id).disabled = pending;
    $("record-start").disabled = pending || !supported;
    $("record-stop").disabled = !recording;
  }
  function release() { clearInterval(timer); timer = null; stream?.getTracks().forEach(t => t.stop()); stream = null; }
  function preview() {
    const player = $("reference-player"); player.pause();
    if (url) URL.revokeObjectURL(url);
    url = selectedFile ? URL.createObjectURL(selectedFile) : null;
    if (url) player.src = url; else { player.removeAttribute("src"); player.load(); }
    player.classList.toggle("hidden", !selectedFile);
    $("transcript-panel").classList.toggle("hidden", !selectedFile);
    $("reference-transcript").value = transcript;
    $("transcript-confirmed").checked = false;
    $("use-transcript").checked = !!selectedFile;
    $("reference-source").textContent = selectedFile ? (recordedStyle ? `録音時の話し方：${recordedStyle}。録音後に設定を変えた場合は、必要に応じて録り直してください。` : `選択中：${selectedFile.name}`) : "";
  }
  function applyStyle() {
    const item = examples[$("voice-style").value];
    if (item) { $("control").value = item[0]; $("reference-script").value = item[1]; }
    $("reference-guidance").textContent = item?.[2] || "話し方を自由に入力し、それに合う例文を編集して録音してください。";
  }
  $("voice-style").onchange = applyStyle;
  $("control").addEventListener("input", () => {
    $("voice-style").value = "custom";
    $("reference-guidance").textContent = "指定した話し方に合わせて例文を編集し、録音してください。";
  });
  $("reference").onchange = () => {
    selectedFile = $("reference").files[0] || null; transcript = ""; recordedStyle = "";
    preview(); status(selectedFile ? "選択した音声を参照用に使用します。" : "参照音声は未選択です。");
  };
  function stop() { if (recorder?.state === "recording") { recording = false; controls(); recorder.stop(); release(); status("録音を準備しています…"); } }
  function cancel() {
    if (pending) status("録音を中断しました。保存済みの参照音声は保持しています。");
    $("reference-player").pause();
    token++; if (recorder?.state === "recording") recorder.stop();
    release(); pending = recording = false; controls();
  }
  $("record-start").onclick = async () => {
    if (pending) return;
    if (!$("reference-script").value.trim()) { status("読み上げスクリプトを入力してください。"); return; }
    const run = ++token;
    const script = $("reference-script").value.trim(), style = $("control").value;
    pending = true; controls(); status("マイクの使用許可を確認しています…");
    try {
      const input = await navigator.mediaDevices.getUserMedia({audio: true});
      if (run !== token) { input.getTracks().forEach(t => t.stop()); return; }
      stream = input;
      const mime = ["audio/webm;codecs=opus", "audio/mp4", "audio/ogg;codecs=opus"].find(t => MediaRecorder.isTypeSupported(t));
      recorder = mime ? new MediaRecorder(stream, {mimeType: mime}) : new MediaRecorder(stream);
      const chunks = [], started = Date.now(), actualMime = recorder.mimeType;
      recorder.ondataavailable = e => { if (e.data.size) chunks.push(e.data); };
      recorder.onerror = () => { cancel(); status("録音に失敗しました。マイクを確認して再試行してください。"); };
      recorder.onstop = () => {
        if (run !== token) return;
        release(); pending = recording = false; controls();
        const type = actualMime || chunks[0]?.type || "audio/webm";
        const blob = new Blob(chunks, {type});
        if (!blob.size) { status("音声を録音できませんでした。もう一度お試しください。"); return; }
        const extension = type.includes("mp4") ? "m4a" : type.includes("ogg") ? "ogg" : "webm";
        selectedFile = new File([blob], `recording.${extension}`, {type});
        transcript = script; recordedStyle = style; $("reference").value = "";
        preview(); status("録音できました。再生して確認してください。録り直す場合はもう一度録音を開始できます。");
      };
      recorder.start(); recording = true; controls(); status("録音中 · 0秒 / 最大30秒");
      timer = setInterval(() => {
        const seconds = Math.floor((Date.now() - started) / 1000);
        status(`録音中 · ${seconds}秒 / 最大30秒`);
        if (seconds >= 30) stop();
      }, 250);
    } catch (error) {
      if (run !== token) return;
      cancel(); status(error.name === "NotAllowedError" ? "マイクが許可されていません。ブラウザーの権限設定を確認するか、音声ファイルを選択してください。" : "マイクを利用できません。接続を確認するか、音声ファイルを選択してください。");
    }
  };
  $("reference-transcript").addEventListener("input", () => { $("transcript-confirmed").checked = false; });
  $("transcribe-reference").onclick = async () => {
    if (pending || !selectedFile) return;
    const run = ++token;
    pending = true; controls(); status("録音を文字起こししています。初回はモデルを準備します…");
    try {
      const body = new FormData(); body.append("reference", selectedFile);
      const response = await fetch("/api/reference-transcription", {method:"POST", body});
      const data = await response.json();
      if (run !== token) return;
      if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "文字起こしに失敗しました");
      $("reference-transcript").value = data.text;
      $("transcript-confirmed").checked = false;
      status("文字起こしできました。録音を聴いて修正し、一致を確認してください。");
    } catch (error) { if (run === token) status(error.message); }
    finally { if (run === token) { pending = false; controls(); } }
  };
  $("record-stop").onclick = stop;
  $("reference-clear").onclick = () => { selectedFile = null; transcript = recordedStyle = ""; $("reference").value = ""; preview(); status("参照音声は未選択です。"); };
  window.referenceRecorder = {
    get busy() { return pending; },
    appendTo(body) {
      if (pending) throw new Error("録音を停止してから制作を保存してください。");
      if (selectedFile) {
        if ($("use-transcript").checked) {
          const text = $("reference-transcript").value.trim();
          if (!text || !$("transcript-confirmed").checked) throw new Error("録音の文字起こしを修正し、一致を確認してください。");
          body.append("reference_transcript", text); body.append("transcript_confirmed", "true");
        }
        body.append("reference", selectedFile); if (transcript) body.append("reference_script", transcript);
      }
    },
    cancel,
    clear() { $("reference-clear").onclick(); }
  };
  window.addEventListener("pagehide", cancel);
  applyStyle(); controls();
  if (!supported) status("この環境では録音できません。localhost または HTTPS で開くか、音声ファイルを選択してください。");
})();
