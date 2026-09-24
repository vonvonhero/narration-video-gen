"""Small LAN-capable review UI for narration generation.

It intentionally uses the standard library: torch and the model stay in the
separate loopback-only Irodori container, while this process only manages jobs
and WAV files.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import wave
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import narration
from . import tts_service


RECENT_JOB_LIMIT = 8
MAX_TASKS = 64
MAX_UPLOAD_BYTES = 64 * 1024 * 1024
UPLOAD_KEEP_SECONDS = 24 * 3600
AUDIO_SUFFIXES = {".wav", ".mp3", ".m4a", ".aac", ".ogg", ".oga", ".opus",
                  ".webm", ".flac", ".aif", ".aiff", ".mp4", ".mov", ".3gp",
                  ".amr", ".wma"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
AUTH_FILE = "web-auth.json"
AUTH_USER = "tts"
AUTH_COOKIE = "nvg_tts_session"
MAX_AUTH_SESSIONS = 64
AUTH_SESSION_SECONDS = 12 * 3600
AUTH_FAILURE_LIMIT = 5
AUTH_FAILURE_WINDOW_SECONDS = 60
MIN_PASSWORD_LENGTH = 10


def auth_path(root):
    return Path(root) / "outputs" / "tts" / AUTH_FILE


def set_password(root, password):
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError("パスワードは%d文字以上にして" % MIN_PASSWORD_LENGTH)
    salt = secrets.token_bytes(16)
    record = {"schema_version": 1, "username": AUTH_USER,
              "salt": salt.hex(), "iterations": 600000,
              "hash": hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 600000).hex()}
    path = auth_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(record), encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def password_configured(root):
    try:
        value = json.loads(auth_path(root).read_text(encoding="utf-8"))
        salt = bytes.fromhex(value["salt"])
        digest = bytes.fromhex(value["hash"])
        iterations = int(value["iterations"])
        if (value.get("username") != AUTH_USER or len(salt) != 16
                or len(digest) != 32 or iterations <= 0):
            return None
        return value
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None


def _password_matches_record(record, username, password):
    if not record or username != AUTH_USER:
        return False
    try:
        expected = bytes.fromhex(record["hash"])
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                     bytes.fromhex(record["salt"]), int(record["iterations"]))
    except (KeyError, TypeError, ValueError):
        return False
    return hmac.compare_digest(actual, expected)


def password_matches(root, username, password):
    return _password_matches_record(password_configured(root), username, password)


_LOGIN_HTML = r"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TTS Web UI ログイン</title>
<style>
:root{color-scheme:light dark;font-family:system-ui,sans-serif}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:#f3f4f6;color:#172033}
main{width:min(88vw,24rem);padding:2rem;border-radius:1rem;background:#fff;box-shadow:0 12px 36px #0002}
h1{font-size:1.35rem;margin:0 0 .7rem}p{line-height:1.6}.fixed{font-size:.9rem;color:#596273}
label{display:block;font-weight:700;margin:1.4rem 0 .45rem}input,button{box-sizing:border-box;width:100%;font:inherit}
input{padding:.75rem;border:1px solid #aeb5c0;border-radius:.55rem}button{margin-top:1rem;padding:.75rem;border:0;border-radius:.55rem;background:#315fc4;color:#fff;font-weight:700;cursor:pointer}
.error{padding:.7rem;border-radius:.5rem;background:#fee2e2;color:#991b1b}
@media(prefers-color-scheme:dark){body{background:#111827;color:#e5e7eb}main{background:#1f2937}.fixed{color:#aeb8c7}.error{background:#4c1d1d;color:#fecaca}}
</style></head><body><main>
<h1>TTS Web UI</h1>
<p class="fixed">ユーザー名は <strong>tts</strong> に固定されています。</p>
__ERROR__
<form method="post" action="/login">
<label for="password">パスワード</label>
<input id="password" name="password" type="password" autocomplete="current-password" required autofocus>
<button type="submit">ログイン</button>
</form></main></body></html>"""


def _own_hostnames():
    names = {"localhost"}
    try:
        hostname = socket.gethostname()
    except OSError:
        return names
    names.add(hostname.lower())
    names.add(hostname.lower().split(".")[0])
    names.add(hostname.lower().split(".")[0] + ".local")
    return names


def host_allowed(header, extra=()):
    """Reject a Host this server was not reached by name or address.

    The page can bind to the LAN, so a browser that can be made
    to resolve an attacker-controlled name to this machine's address would
    otherwise be same-origin with it, read the request token out of the page
    and drive generation and adoption. Every legitimate way in uses an
    address literal or this machine's own name.
    """
    if not header:
        return False
    name = header.strip().rsplit(":", 1)[0] if not header.startswith("[") \
        else header.split("]", 1)[0].lstrip("[")
    name = name.strip().lower().rstrip(".")
    if name in _own_hostnames() or name in {str(item).lower() for item in extra}:
        return True
    for family in (socket.AF_INET, socket.AF_INET6):
        try:
            socket.inet_pton(family, name)
            return True
        except OSError:
            continue
    return False

_HTML = r"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ナレーション作成</title>
<style>
:root{
  --bg:#f5f6f8; --surface:#ffffff; --surface-2:#f8f9fb; --text:#171a1f;
  --muted:#5d6875; --border:#e2e6ec; --border-strong:#cdd4dd;
  --accent:#2563eb; --accent-text:#ffffff; --accent-soft:#e8f0ff;
  --ok:#0a7d43; --ok-soft:#e4f6ec; --warn:#a45606; --warn-soft:#fdf0e2;
  --danger:#b42318; --danger-soft:#fdeceb; --shadow:0 1px 2px rgba(16,24,40,.06),0 8px 24px rgba(16,24,40,.05);
}
@media (prefers-color-scheme:dark){
  :root{
    --bg:#101318; --surface:#191d24; --surface-2:#1f242c; --text:#e8ebf0;
    --muted:#98a2b0; --border:#2b313b; --border-strong:#3a424f;
    --accent:#5b93ff; --accent-text:#0b0f15; --accent-soft:#1b2740;
    --ok:#4ade80; --ok-soft:#152a1f; --warn:#f0b45f; --warn-soft:#2c2317;
    --danger:#ff8078; --danger-soft:#2e1a19; --shadow:0 1px 2px rgba(0,0,0,.4);
  }
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
  font-family:system-ui,-apple-system,"Segoe UI","Hiragino Sans","Noto Sans JP",sans-serif;
  font-size:15px;line-height:1.7;-webkit-text-size-adjust:100%}
.wrap{max-width:860px;margin:0 auto;padding:24px 16px 88px}
header{padding:8px 0 4px}
h1{font-size:1.45rem;margin:0 0 4px;letter-spacing:.01em}
h2{font-size:1.05rem;margin:0;display:flex;align-items:center;gap:10px}
h3{font-size:.95rem;margin:20px 0 8px}
p{margin:.5em 0}
.sub{color:var(--muted);margin:0 0 18px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:14px;
  padding:18px 18px 20px;margin:14px 0;box-shadow:var(--shadow)}
.step{display:inline-flex;align-items:center;justify-content:center;width:26px;height:26px;
  border-radius:50%;background:var(--accent-soft);color:var(--accent);font-size:.85rem;
  font-weight:700;flex:none}
.card.done .step{background:var(--ok-soft);color:var(--ok)}
.card[aria-disabled=true]{opacity:.55}
.muted{color:var(--muted);font-size:.88rem}
.field{display:block;margin:14px 0 0}
.field>span{display:block;font-weight:600;font-size:.9rem;margin-bottom:6px}
input,select,textarea,button{font:inherit;color:inherit}
input[type=text],input[type=number],select,textarea{
  width:100%;background:var(--surface-2);border:1px solid var(--border-strong);
  border-radius:9px;padding:10px 12px}
textarea{min-height:190px;resize:vertical;line-height:1.9}
input:focus-visible,select:focus-visible,textarea:focus-visible,button:focus-visible{
  outline:2px solid var(--accent);outline-offset:2px}
button{cursor:pointer;border-radius:9px;padding:10px 16px;border:1px solid var(--border-strong);
  background:var(--surface-2);font-weight:600}
button:hover:not(:disabled){border-color:var(--accent)}
button:disabled{cursor:not-allowed;opacity:.5}
button.primary{background:var(--accent);color:var(--accent-text);border-color:transparent}
button.big{width:100%;padding:14px;font-size:1.02rem}
button.small{padding:6px 11px;font-size:.85rem;font-weight:500}
.pill{display:inline-flex;align-items:center;gap:6px;border-radius:999px;padding:3px 11px;
  font-size:.82rem;font-weight:600;background:var(--surface-2);border:1px solid var(--border)}
.pill.ok{background:var(--ok-soft);color:var(--ok);border-color:transparent}
.pill.warn{background:var(--warn-soft);color:var(--warn);border-color:transparent}
.dot{width:7px;height:7px;border-radius:50%;background:currentColor;flex:none}
.status-row{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0}
.notice{border-radius:10px;padding:10px 13px;margin:12px 0;font-size:.9rem;
  background:var(--surface-2);border:1px solid var(--border)}
.notice.warn{background:var(--warn-soft);color:var(--warn);border-color:transparent}
.notice.err{background:var(--danger-soft);color:var(--danger);border-color:transparent}
.notice.ok{background:var(--ok-soft);color:var(--ok);border-color:transparent}
.choice{display:flex;gap:8px;flex-wrap:wrap}
.choice button{flex:1 1 130px;padding:12px}
.choice button[aria-pressed=true]{background:var(--accent-soft);border-color:var(--accent);color:var(--accent)}
.preview{margin-top:12px;border-top:1px dashed var(--border);padding-top:10px}
.preview ol{margin:6px 0 0;padding-left:1.4em}
.preview li{margin:3px 0}
.preview li span{color:var(--muted);font-size:.82rem;margin-left:6px}
.part{border:1px solid var(--border);border-radius:12px;padding:14px;margin:12px 0;
  background:var(--surface-2)}
.part-head{display:flex;gap:8px;align-items:baseline;font-weight:600;flex-wrap:wrap}
.part-no{color:var(--muted);font-variant-numeric:tabular-nums;flex:none}
audio{width:100%;margin-top:10px;height:38px}
.tools{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:12px}
.tools label{display:inline-flex;align-items:center;gap:6px;font-size:.86rem;color:var(--muted)}
.tools input[type=number]{width:9.5em}
.adopt{background:var(--surface);border-top:1px solid var(--border);
  margin:18px -18px -20px;padding:14px 18px;border-radius:0 0 14px 14px}
.check{display:flex;gap:10px;align-items:flex-start;font-size:.92rem;margin-bottom:10px}
.check input{margin-top:5px;width:18px;height:18px;flex:none;accent-color:var(--accent)}
.recent button{display:block;width:100%;text-align:left;margin:6px 0;font-weight:500;
  background:var(--surface-2)}
.library-item{border:1px solid var(--border);border-radius:12px;padding:14px;margin:12px 0;
  background:var(--surface-2)}
.library-head{display:flex;gap:8px;align-items:baseline;justify-content:space-between;
  flex-wrap:wrap}.library-head strong{font-size:.96rem}.library-meta{color:var(--muted);font-size:.82rem}
.library-item details{margin-top:10px;padding-top:8px}.library-item pre{white-space:pre-wrap;
  word-break:break-word;font:inherit;font-size:.88rem;margin:8px 0 0}
.voice{display:flex;align-items:center;gap:6px;flex:1 1 150px}
.voice>button:first-child{flex:1;min-width:0;padding:10px 12px;display:flex;
  align-items:center;gap:10px;text-align:left}
.voice .thumb{width:48px;height:27px;border-radius:4px;object-fit:cover;flex:none;
  background:var(--border)}
.voice .name{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.voice .play{flex:none;padding:12px 10px;font-size:.8rem}
.voice .made{font-size:.72rem;font-weight:500;opacity:.75;display:block}
.maker{border:1px solid var(--accent);border-radius:12px;padding:16px;margin-top:14px;
  background:var(--surface-2)}
.crop{width:100%;max-width:340px;aspect-ratio:16/9;border-radius:10px;overflow:hidden;
  border:1px solid var(--border-strong);background:#000;margin-top:8px}
.crop img{width:100%;height:100%;object-fit:cover;display:block}
.drop{border:1.5px dashed var(--border-strong);border-radius:10px;padding:14px;
  text-align:center;color:var(--muted);font-size:.9rem;cursor:pointer}
.drop.on{border-color:var(--accent);color:var(--accent);background:var(--accent-soft)}
.radio{display:flex;gap:8px;flex-wrap:wrap;margin-top:6px}
.radio button{flex:1 1 140px}
.radio button[aria-pressed=true]{background:var(--accent-soft);border-color:var(--accent);
  color:var(--accent)}
.actions{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:10px;
  padding-top:10px;border-top:1px dashed var(--border)}
.actions .who{color:var(--muted);font-size:.86rem;margin-right:auto}
.danger{color:var(--danger);border-color:var(--danger)}
.recent .when{color:var(--muted);font-size:.82rem}
details{margin-top:16px;border-top:1px solid var(--border);padding-top:12px}
details.redo{margin-top:10px;border-top:1px dashed var(--border);padding-top:8px}
details.redo summary{font-weight:500;font-size:.86rem;color:var(--muted)}
summary{cursor:pointer;font-weight:600;font-size:.92rem}
details ul{padding-left:1.2em}
code{background:var(--surface-2);border:1px solid var(--border);border-radius:6px;
  padding:1px 6px;font-size:.86em;overflow-wrap:anywhere}
.spin{display:inline-block;width:13px;height:13px;border:2px solid currentColor;
  border-right-color:transparent;border-radius:50%;animation:sp .7s linear infinite;
  vertical-align:-2px;margin-right:7px}
@keyframes sp{to{transform:rotate(360deg)}}
@media (prefers-reduced-motion:reduce){.spin{animation:none}}
@media (max-width:560px){.wrap{padding:16px 12px 72px}.card{padding:15px}.tools{gap:6px}}
</style></head>
<body><div class="wrap">
<header>
<h1>ナレーション作成</h1>
<p class="sub">台本からナレーションを作り、聴いてから動画に使えます。</p>
</header>

<div id="flash" role="status" aria-live="polite" hidden></div>

<section class="card" id="card-setup">
<h2><span class="step">1</span>準備</h2>
<div class="status-row" id="status-row"><span class="pill">確認中…</span></div>
<div id="setup-action"></div>
<p class="muted" id="setup-note">初回はモデル一式（約3.7GB・MITライセンス）をダウンロードします。CPUでの生成には数分かかることがあります。</p>
</section>

<section class="card" id="card-character">
<h2><span class="step">2</span>キャラクターを選ぶ</h2>
<div class="field"><span>声とキャラクター</span><div class="choice" id="character"></div></div>
<div id="character-actions"></div>
<div id="maker" hidden></div>
</section>

<section class="card" id="card-script">
<h2><span class="step">3</span>台本を書く</h2>
<label class="field"><span>台本</span>
<textarea id="script" placeholder="ここに台本を入力します。&#10;改行で音声パートを分けます。長い行は句読点でも分かれます。&#10;&#10;空行を入れると、文の間が長くなります。"></textarea></label>
<div class="preview" id="preview" hidden></div>
<details id="advanced"><summary>話し方などの詳細設定</summary>
<label class="field"><span>話し方</span><input id="caption" type="text"></label>
<label class="field"><span>最後の余韻（ミリ秒）</span><input id="outro" type="number" min="0" max="5000" step="100" value="1500"></label>
<label class="check" style="margin-top:14px"><input id="asr" type="checkbox" checked>
<span>音声認識で台本と照合する（推奨）</span></label>
</details>
<p style="margin-top:18px"><button id="generate" class="primary big">音声を作る</button></p>
</section>

<section class="card" id="card-review">
<h2><span class="step">4</span>聴いて仕上げる</h2>
<div id="result" class="muted">まだ音声はありません。台本を書いて「音声を作る」を押してください。</div>
</section>

<section class="card" id="card-library" hidden>
<h2><span id="library-character"></span>の動画に使える音声・台本</h2>
<p class="muted">ここの内容が、動画生成メニューの <code>run</code> で選べます。</p>
<div id="library"></div>
</section>

<section class="card recent" id="card-recent" hidden>
<h2><span id="recent-character"></span>の作成履歴</h2>
<p class="muted">試聴や作り直しのための生成履歴です。動画で使えるかは状態を確認してください。</p>
<div id="recent"></div>
</section>

<details class="card"><summary>うまくいかないときの調整のしかた</summary>
<ul>
<li><b>読み方そのものが違う:</b> まず台本を直します。むずかしい漢字はひらがなにすると安定します。</li>
<li><b>抑揚やアクセントが不自然:</b> そのパートの<b>seed</b>を変えて作り直します。「最初のseedに戻す」で初期値を使えます。</li>
<li><b>そのパートだけ間延びする・速すぎる:</b> <b>長さ</b>を0.85〜1.15くらいで試します。0.8で約20%短く、1.2で約20%長くなります。</li>
<li><b>文と文の間だけ変えたい:</b> <b>次の文まで</b>を直して「間の変更を反映する」を押します。</li>
<li><b>全体の雰囲気を変えたい:</b> 「話し方などの詳細設定」の文章を書き換えて、もう一度「音声を作る」を押します。</li>
</ul>
</details>
</div>

<script>
const csrf=__CSRF__, defaults=__DEFAULTS__, defaultCaption=__CAPTION__, designCaption=__DESIGN__;
let current=null, character=Object.keys(defaults)[0], busyCount=0, config=null;
let libraryInputs=[], recentJobs=[], savedListsLoaded=false;
const $=id=>document.getElementById(id);
const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const headers={'Content-Type':'application/json','X-NVG-CSRF':csrf};

const ASR_LABEL={
  pass:['ok','台本どおり',''],
  fail_extra_speech:['warn','要確認','音声認識で台本にない発話が見つかりました。聴いて確認してください'],
  fail_missing_speech:['warn','要確認','音声認識で読み飛ばしが疑われました。聴いて確認してください'],
  fail_mismatch:['warn','要確認','音声認識の結果が台本と大きく異なります'],
  review_asr_mismatch:['warn','要確認','音声認識の結果が台本と異なります。表記の違いも含みます'],
  error:['warn','未確認','音声認識に失敗しました'],
  not_run:['','未確認','']};
const ISSUE_LABEL=[
  [/^short text has unusually long audio/,'短い文なのに音声が長めです。余計な発話がないか聴いてください'],
  [/^ASR result requires listening review/,'音声認識の結果に差があります。聴いて確認してください'],
  [/^expected mono audio/,'モノラルではありません'],
  [/^expected 16-bit PCM audio/,'16bit PCMではありません'],
  [/^expected 48000 Hz audio/,'48kHzではありません'],
  [/^audio is unexpectedly short/,'音声が極端に短いです']];
const issueText=v=>{for(const [re,ja] of ISSUE_LABEL) if(re.test(v)) return ja; return v};

function flash(kind,text){const el=$('flash');if(!text){el.hidden=true;el.innerHTML='';return}
  el.className='notice '+kind;el.innerHTML=esc(text);el.hidden=false;
  if(kind!=='err')setTimeout(()=>{if(el.textContent===text)el.hidden=true},6000)}

async function api(path, body){
  let r, failure;
  const attempts=body?1:3;
  for(let attempt=0;attempt<attempts;attempt++){
    try{r=await fetch(path,{method:body?'POST':'GET',headers,body:body?JSON.stringify(body):undefined});failure=null;break}
    catch(e){failure=e;if(attempt+1<attempts)await new Promise(resolve=>setTimeout(resolve,400*(attempt+1)))}
  }
  if(failure)throw Error('一時的に接続できませんでした。少し待ってからもう一度試してください');
  let j; try{j=await r.json()}catch(e){throw Error('サーバーから応答を読めませんでした')}
  if(!r.ok)throw Error(j.error||r.statusText); return j}

function syncAdopt(){const a=$('adopt'),l=$('listened');
  if(a&&l)a.disabled=!l.checked||busyCount>0}
const disabledBeforeWork=new WeakMap();
function syncControls(){document.querySelectorAll('button,input,select,textarea').forEach(el=>{
  if(busyCount>0){if(!disabledBeforeWork.has(el))disabledBeforeWork.set(el,el.disabled);el.disabled=true}
  else if(disabledBeforeWork.has(el)){el.disabled=disabledBeforeWork.get(el);disabledBeforeWork.delete(el)}});
  syncAdopt()}
function setBusy(on){busyCount=Math.max(0,busyCount+(on?1:-1));syncControls()}
async function work(label,fn){setBusy(true);const el=$('setup-action');
  try{return await fn(m=>{if(label!==null)el.innerHTML='<p class="notice"><span class="spin"></span>'+esc(m||label)+'</p>'})}
  finally{const waiting=$('working');if(waiting)waiting.remove();
    if($('a-status'))$('a-status').innerHTML='';
    setBusy(false);if(label!==null)renderSetup()}}

/* ---- step 1: engine ---- */
function renderSetup(){
  const row=$('status-row'), act=$('setup-action');
  if(!config){row.innerHTML='<span class="pill">確認中…</span>';act.innerHTML='';return}
  const model=config.model_prepared, live=config.backend.reachable;
  $('setup-note').hidden=model;
  row.innerHTML=
    `<span class="pill ${model?'ok':'warn'}"><span class="dot"></span>モデル: ${model?'準備ずみ':'未ダウンロード'}</span>`+
    `<span class="pill ${live?'ok':'warn'}"><span class="dot"></span>音声エンジン: ${live?'動作中':'停止中'}</span>`;
  $('card-setup').classList.toggle('done', model&&live);
  if(!model){act.innerHTML='<button id="prepare" class="primary big">モデルをダウンロードする（約3.7GB）</button>';
    $('prepare').onclick=()=>doPrepare().catch(e=>flash('err',e.message))}
  else if(!live){act.innerHTML='<button id="start" class="primary big">音声エンジンを起動する</button>';
    $('start').onclick=()=>doStart().catch(e=>flash('err',e.message))}
  else act.innerHTML='<p class="notice ok">準備できています。台本を書いて「音声を作る」を押してください。</p>';
  syncControls();
}
async function refresh(){config=await api('/api/config');renderSetup();renderCharacters()}
async function poll(id,onMessage){for(;;){const j=await api('/api/tasks/'+id);
  if(onMessage&&j.message)onMessage(j.message);
  if(j.status==='failed')throw Error(j.error||'処理に失敗しました');
  if(j.status==='completed')return j.result;
  await new Promise(r=>setTimeout(r,800))}}
async function doPrepare(){await work('モデルをダウンロード中…',async say=>{
  const t=await api('/api/prepare',{});await poll(t.task_id,say);await refresh()});
  flash('ok','モデルの準備ができました。')}
async function doStart(){await work('音声エンジンを起動中…',async say=>{
  const t=await api('/api/backend/start',{device:'auto'});await poll(t.task_id,say);await refresh()})}
async function ensureReady(say){
  await refresh();
  if(!config.model_prepared){say('モデルをダウンロード中…');
    const t=await api('/api/prepare',{});await poll(t.task_id,say);await refresh()}
  if(!config.backend.reachable){say('音声エンジンを起動中…');
    const t=await api('/api/backend/start',{device:'auto'});await poll(t.task_id,say);await refresh()}}

/* ---- step 2: script ---- */
function renderCharacters(){
  const pending=(config&&config.pending_characters)||{};
  const pendingCount=Object.keys(pending).length;
  $('character').innerHTML=Object.entries(defaults).map(([key,v])=>
    `<div class="voice"><button type="button" data-char="${esc(key)}" aria-pressed="${key===character}" data-keep="1">`+
    `<img class="thumb" alt="" src="/character/${encodeURIComponent(key)}/portrait">`+
    `<span class="name">${esc(v.label_ja||key)}${v.source==='user'?`<span class="made">${v.derived_from?esc((defaults[v.derived_from]||{}).label_ja||v.derived_from)+'から作成':v.designed?'文章から作成':'音声から作成'}</span>`:''}</span></button>`+
    `<button type="button" class="play" data-play="${esc(key)}" data-keep="1" title="この声を聴く">▶</button></div>`).join('')+
    `<div class="voice"><button type="button" id="new-character" data-keep="1">＋ キャラクターを作る${pendingCount?' (作成途中 '+pendingCount+'件)':''}</button></div>`;
  document.querySelectorAll('#character button[data-char]').forEach(b=>b.onclick=()=>{
    const changed=character!==b.dataset.char;
    character=b.dataset.char;renderCharacters();
    $('caption').value=defaults[character].caption;closeMaker();
    if(changed){current=null;$('result').className='muted';
      $('result').textContent='このキャラクターでは、まだ音声を開いていません。';
      if(location.hash.startsWith('#job='))history.replaceState(null,'',location.pathname)}});
  document.querySelectorAll('#character button[data-play]').forEach(b=>b.onclick=()=>{
    const a=new Audio('/character/'+encodeURIComponent(b.dataset.play)+'/reference');a.play()});
  $('new-character').onclick=openMaker;
  renderActions();renderSavedLists();syncControls()}

/* ---- acting on the character that is selected ---- */
function renderActions(){
  const box=$('character-actions'), v=defaults[character];
  if(!v||v.source!=='user'){box.innerHTML='';return}
  box.innerHTML=`<div class="actions">
    <span class="who">${esc(v.label_ja||character)}</span>
    ${v.designed?'<button class="small" id="a-voice">声を作り直す</button>':''}
    <button class="small" id="a-image">画像を差し替える</button>
    <button class="small danger" id="a-delete">削除する</button></div>
    <div id="a-panel"></div>`;
  if(v.designed)$('a-voice').onclick=openRedesign;
  $('a-image').onclick=openPortraitSwap;
  $('a-delete').onclick=confirmDelete}

function confirmDelete(){
  const v=defaults[character];
  $('a-panel').innerHTML=`<div class="maker">
    <p><b>${esc(v.label_ja||character)}</b> を削除します。参照音声と画像が消えます。
    このキャラクターで作った音声が残っている場合は削除できません。</p>
    <div class="tools"><button class="danger" id="a-del-yes">削除する</button>
    <button id="a-del-no">やめる</button></div></div>`;
  $('a-del-no').onclick=()=>{$('a-panel').innerHTML=''};
  $('a-del-yes').onclick=()=>work(null,async()=>{
    await api('/api/characters/'+encodeURIComponent(character)+'/delete',{});
    await refreshCharacters(Object.keys(defaults)[0]);
    flash('ok','削除しました。')}).catch(e=>flash('err',e.message))}

function openPortraitSwap(){
  $('a-panel').innerHTML=`<div class="maker">
    <div class="drop" id="a-image-drop">新しい画像をここにドロップ、またはクリックして選ぶ</div>
    <div class="crop" id="a-crop" hidden><img id="a-crop-img" alt=""></div>
    <p class="muted">動画はこの枠の見え方（16:9・中央）で始まります。</p>
    <div class="tools"><button class="primary" id="a-image-save" disabled>この画像にする</button>
    <button id="a-image-cancel">やめる</button></div></div>`;
  let picked=null;
  dropTarget('a-image-drop','image/*','image',id=>{
    picked=id;$('a-image-save').disabled=!id},'a-crop','a-crop-img');
  $('a-image-cancel').onclick=()=>{$('a-panel').innerHTML=''};
  $('a-image-save').onclick=()=>work(null,async()=>{
    await api('/api/characters/'+encodeURIComponent(character)+'/portrait',
              {image_upload:picked});
    await refreshCharacters(character);
    flash('ok','画像を差し替えました。')}).catch(e=>flash('err',e.message))}

function openRedesign(){
  const v=defaults[character];
  $('a-panel').innerHTML=`<div class="maker">
    <p class="muted">名前も画像もそのままで、声だけを作り直します。</p>
    <label class="field"><span>どんな声で、どんな話し方か</span>
      <input id="a-caption" type="text" value="${esc(v.caption||'')}"></label>
    <div class="field"><span>声の個体差</span><div class="tools" style="margin-top:0">
      <input id="a-seed" type="number" min="0" max="4294967295" step="1"
        value="${Number(v.design_seed??v.seed??0)}" style="width:12em">
      <button type="button" id="a-reroll">別の声にする</button></div></div>
    <div id="a-status"></div>
    <div class="tools"><button class="primary" id="a-voice-go">作り直して試し聞き</button>
    <button id="a-voice-cancel">やめる</button></div>
    <div id="a-test"></div></div>`;
  $('a-reroll').onclick=()=>{$('a-seed').value=newSeed()};
  $('a-voice-cancel').onclick=()=>{$('a-panel').innerHTML=''};
  $('a-voice-go').onclick=()=>redesign().catch(e=>flash('err',e.message))}

async function redesign(){
  const id=character;
  await work(null,async()=>{
    $('a-status').innerHTML='<p class="notice"><span class="spin"></span>声を作り直しています…</p>';
    const t=await api('/api/characters/'+encodeURIComponent(id)+'/voice',
      {caption:$('a-caption').value,seed:Number($('a-seed').value)});
    const made=await poll(t.task_id,m=>{
      $('a-status').innerHTML='<p class="notice"><span class="spin"></span>'+esc(m)+'</p>'});
    await refreshCharacters(id);openRedesign();
    $('a-test').innerHTML=`<h3>試し聞き</h3>
      <audio controls autoplay src="/media/${encodeURIComponent(made.test.job_id)}/narration.wav?v=${Date.now()}"></audio>
      ${(made.advice||[]).map(v=>`<p class="notice warn">${esc(v)}</p>`).join('')}
      <p class="muted">気に入らなければ「別の声にする」を押して、もう一度作り直せます。</p>`;
    syncControls()})}

/* ---- making a character ---- */
let maker={mode:'design',voiceFrom:'',audioUpload:null,imageUpload:null,seed:''};
function closeMaker(){$('maker').hidden=true;$('maker').innerHTML='';$('card-script').hidden=false}
function openMaker(){
  maker={mode:'design',voiceFrom:'',audioUpload:null,imageUpload:null,seed:''};
  $('card-script').hidden=true;
  const bases=Object.entries(defaults).map(([key,v])=>
    `<button type="button" data-mode="inherit" data-base="${esc(key)}">${esc(v.label_ja||key)}を引き継ぐ</button>`).join('');
  const pending=Object.entries((config&&config.pending_characters)||{});
  const pendingHtml=pending.length?`<div class="notice warn"><b>作成途中のキャラクター</b>
    <p class="muted">前回、確定前に画面を閉じたキャラクターです。保存するか、不要なら削除してください。</p>
    ${pending.map(([id,v])=>`<div class="tools"><span class="who">${esc(v.label_ja||id)}</span>
      <button type="button" class="small pending-play" data-id="${esc(id)}">声を聞く</button>
      <button type="button" class="small pending-keep" data-id="${esc(id)}" ${v.busy?'disabled':''}>保存して使う</button>
      <button type="button" class="small danger pending-delete" data-id="${esc(id)}" ${v.busy?'disabled':''}>削除</button>
      ${v.busy?'<span class="muted">試し聞きを生成中です。少し待って画面を更新してください。</span>':''}</div>`).join('')}</div>`:'';
  $('maker').hidden=false;
  $('maker').innerHTML=`${pendingHtml}<div class="maker">
  <h3 style="margin-top:0">キャラクターを作る</h3>
  <p class="muted">名前・声・話し方・画像を決めます。作ったあと、その場で試し聞きできます。</p>
  <label class="field"><span>名前</span><input id="m-label" type="text" placeholder="例: ギャル葵"></label>
  <div class="field"><span>声</span><div class="radio" id="m-base">
    <button type="button" data-mode="design" data-base="">声を文章で作る</button>
    <button type="button" data-mode="upload" data-base="">音声を指定する</button>${bases}</div>
    <p class="muted" id="m-base-note"></p></div>
  <div id="m-audio" hidden>
    <div class="drop" id="m-audio-drop">音声ファイルをここにドロップ、またはクリックして選ぶ<br>
      <span class="muted">10秒以上あると安定します。mp3、m4a、wavなど</span></div>
    <label class="check" style="margin-top:10px"><input id="m-consent" type="checkbox" data-keep="1">
    <span>この声は自分の声、または本人からはっきり許可をもらった声です。</span></label>
  </div>
  <label class="field"><span id="m-caption-label">話し方</span><input id="m-caption" type="text"></label>
  <div class="field" id="m-design-method-row" hidden><span>声の作り方</span>
    <select id="m-design-method"><option value="no_ref">参照なしで新しい声を作る</option>
    <option value="synthetic_reference">同梱の合成音声を土台に作る</option></select></div>
  <div class="field" id="m-seed-row" hidden><span>声の個体差</span>
    <div class="tools" style="margin-top:0">
      <input id="m-seed" type="number" min="0" max="4294967295" step="1" style="width:12em">
      <button type="button" id="m-reroll">別の声にする</button>
      <span class="muted">同じ文章でも、この数字が違うと別人の声になります。</span>
    </div></div>
  <div class="field"><span>画像<span class="muted" id="m-image-note"></span></span>
    <div class="drop" id="m-image-drop">画像をここにドロップ、またはクリックして選ぶ</div>
    <div class="crop" id="m-crop" hidden><img id="m-crop-img" alt=""></div>
    <p class="muted">動画はこの枠の見え方（16:9・中央）で始まります。顔が切れていないか確かめてください。</p>
  </div>
  <div id="m-status"></div>
  <div class="tools" style="margin-top:14px">
    <button id="m-create" class="primary">作って試し聞き</button>
    <button id="m-cancel">やめる</button>
  </div></div>`;
  $('m-cancel').onclick=closeMaker;
  $('m-create').onclick=()=>createCharacter().catch(e=>makerStatus('err',e.message));
  document.querySelectorAll('.pending-keep').forEach(b=>b.onclick=()=>
    keepCharacter(b.dataset.id).catch(e=>makerStatus('err',e.message)));
  document.querySelectorAll('.pending-delete').forEach(b=>b.onclick=()=>
    deleteCharacter(b.dataset.id).catch(e=>makerStatus('err',e.message)));
  document.querySelectorAll('.pending-play').forEach(b=>b.onclick=()=>
    new Audio('/character/'+encodeURIComponent(b.dataset.id)+'/reference').play());
  document.querySelectorAll('#m-base button').forEach(b=>b.onclick=()=>{
    maker.mode=b.dataset.mode;maker.voiceFrom=b.dataset.base;renderMakerBase()});
  $('m-design-method').onchange=renderMakerBase;
  $('m-reroll').onclick=()=>{maker.seed='';$('m-seed').value=newSeed()};
  dropTarget('m-image-drop','image/*','image',id=>{maker.imageUpload=id});
  dropTarget('m-audio-drop','audio/*,video/*','audio',id=>{maker.audioUpload=id});
  renderMakerBase()}
function newSeed(){return Math.floor(Math.random()*4294967296)}
function renderMakerBase(){
  document.querySelectorAll('#m-base button').forEach(b=>
    b.setAttribute('aria-pressed', String(
      b.dataset.mode===maker.mode && b.dataset.base===maker.voiceFrom)));
  const design=maker.mode==='design', upload=maker.mode==='upload';
  $('m-audio').hidden=!upload;
  $('m-seed-row').hidden=!design;
  $('m-design-method-row').hidden=!design;
  if(design&&!$('m-seed').value)$('m-seed').value=newSeed();
  $('m-caption-label').textContent=design?'どんな声で、どんな話し方か':'話し方';
  $('m-caption').value=design?designCaption
    :upload?defaultCaption:((defaults[maker.voiceFrom]||{}).caption||'');
  $('m-base-note').textContent=design
    ?($('m-design-method').value==='no_ref'
      ?'既存の音声を使わず、文章から新しい声を作ります。まず表示された説明文で試せます。説明文によって音質に差が出るため、試し聞きして確認してください。'
      :'同梱の合成音声を土台に、声や話し方を変えて作ります。声質は土台の声に近くなることがあります。')
    :upload?'話している音声から声を写し取ります。10秒以上あると安定します。'
    :'声はそのままで、話し方と画像だけを変えられます。';
  $('m-image-note').textContent=maker.voiceFrom?'　省略すると元のキャラクターの画像を使います':'';}
function makerStatus(kind,text){
  const target=$('m-preview-status')||$('m-status');if(!target){flash(kind,text);return}
  target.innerHTML=text?`<p class="notice ${kind}">${kind==='wait'?'<span class="spin"></span>':''}${esc(text)}</p>`:''}
function dropTarget(id,accept,kind,onDone,cropId,cropImgId){
  const box=$(id);
  const input=document.createElement('input');
  input.type='file';input.accept=accept;input.style.display='none';
  box.after(input);
  const take=async file=>{
    if(!file||busyCount)return;
    onDone(null);
    box.classList.remove('on');
    box.textContent=file.name+' を読み込んでいます…';
    try{
      const j=await work(null,async()=>{
        const r=await fetch('/api/uploads',{method:'POST',body:file,headers:{
          'X-NVG-CSRF':csrf,'X-NVG-Kind':kind,'X-NVG-Filename':encodeURIComponent(file.name)}});
        const data=await r.json();
        if(!r.ok)throw Error(data.error||r.statusText);return data});
      onDone(j.upload_id);
      box.textContent=file.name+' ✓　クリックで選び直す';
      if(kind==='image'){const c=$(cropId||'m-crop'),i=$(cropImgId||'m-crop-img');
        if(c&&i){c.hidden=false;i.src=URL.createObjectURL(file)}}
    }catch(e){box.textContent='読み込めませんでした。別のファイルを選んでください';
      makerStatus('err',e.message)}};
  box.tabIndex=0;box.setAttribute('role','button');
  box.onclick=()=>{if(!busyCount)input.click()};
  box.onkeydown=e=>{if((e.key==='Enter'||e.key===' ')&&!busyCount){e.preventDefault();input.click()}};
  input.onchange=()=>take(input.files[0]);
  box.ondragover=e=>{e.preventDefault();box.classList.add('on')};
  box.ondragleave=()=>box.classList.remove('on');
  box.ondrop=e=>{e.preventDefault();if(!busyCount)take(e.dataTransfer.files[0])}}
async function createCharacter(){
  const label=$('m-label').value.trim();
  if(!label)throw Error('名前を入力してください');
  if(maker.mode==='upload'){
    if(!maker.audioUpload)throw Error('音声ファイルを選んでください');
    if(!$('m-consent').checked)
      throw Error('この声を使ってよいという確認にチェックを入れてください')}
  if(maker.mode==='design'&&!$('m-caption').value.trim())
    throw Error('どんな声かを文章で書いてください');
  if(maker.mode!=='inherit'&&!maker.imageUpload)throw Error('画像を1枚選んでください');
  await work(null,async()=>{
    makerStatus('wait','キャラクターを作っています…');
    await ensureReady(m=>makerStatus('wait',m));
    const t=await api('/api/characters',{label,caption:$('m-caption').value,
      design_method:$('m-design-method').value,mode:maker.mode,design_seed:maker.mode==='design'?Number($('m-seed').value):null,
      voice_from:maker.voiceFrom||null,audio_upload:maker.audioUpload,
      image_upload:maker.imageUpload,consent:$('m-consent')?$('m-consent').checked:false});
    const made=await poll(t.task_id,m=>makerStatus('wait',m));
    await refreshCharacters(made.id);
    makerStatus('','');
    renderMade(made)})}

function renderMade(made){
  const designed=!!made.designed_from||made.seed!==undefined;
  const form=$('maker').querySelector('.maker');if(form)form.hidden=true;
  const old=$('m-made'); if(old)old.remove();
  $('maker').insertAdjacentHTML('beforeend',`<div class="maker" id="m-made">
    <h3 style="margin-top:0">試し聞き</h3>
    <p><b>${esc(made.label)}</b> ができました。試し聞きして、この声に決めたら「このキャラクターにする」を押してください。</p>
    <p class="muted">キャラクターを確定すると、台本を書けるようになります。</p>
    ${designed?`<p class="muted">声の個体差: ${esc(String((made.designed_from||made).seed))}</p>`:''}
    <audio controls autoplay src="/media/${encodeURIComponent(made.test.job_id)}/narration.wav?v=${Date.now()}"></audio>
    ${(made.advice||[]).map(v=>`<p class="notice warn">${esc(v)}</p>`).join('')}
    <div id="m-preview-status"></div>
    <div class="tools" style="margin-top:12px">
      <button id="m-keep" class="primary">このキャラクターにする</button>
      ${designed?'<button id="m-again">別の声にする</button>':''}
      <button id="m-scrap">やめる</button>
    </div></div>`);
  $('m-keep').onclick=()=>keepCharacter(made.id).catch(e=>makerStatus('err',e.message));
  $('m-scrap').onclick=()=>deleteCharacter(made.id,false).catch(e=>makerStatus('err',e.message));
  if($('m-again'))$('m-again').onclick=()=>rerollMade(made.id).catch(e=>makerStatus('err',e.message));
  syncControls()}

async function rerollMade(id){
  // Keep the existing name and portrait while replacing only the voice.
  const seed=newSeed(); $('m-seed').value=seed;
  await work(null,async()=>{
    makerStatus('wait','別の声を作っています…');
    const t=await api('/api/characters/'+encodeURIComponent(id)+'/voice',
      {caption:$('m-caption').value,seed});
    const made=await poll(t.task_id,m=>makerStatus('wait',m));
    makerStatus('','');
    renderMade(Object.assign({label:made.label},made))})}
async function keepCharacter(id){
  await work(null,async()=>{
    makerStatus('wait','キャラクターを保存しています…');
    await api('/api/characters/'+encodeURIComponent(id)+'/commit',{});
    await refreshCharacters(id);
    closeMaker();flash('ok','キャラクターを保存しました。')})}
async function refreshCharacters(select){
  const c=await api('/api/config');config=c;
  for(const key of Object.keys(defaults))delete defaults[key];
  Object.assign(defaults,c.characters);
  if(select&&defaults[select])character=select;
  if(!defaults[character])character=Object.keys(defaults)[0];
  renderCharacters();$('caption').value=(defaults[character]||{}).caption||''}
async function deleteCharacter(id,reopen=true){
  await work(null,async()=>{
    await api('/api/characters/'+encodeURIComponent(id)+'/delete',{});
    await refreshCharacters(Object.keys(defaults)[0]);
    closeMaker();
    if(reopen)openMaker();else flash('ok','作成途中のキャラクターを削除しました。')})}
function outroValue(){const value=Number($('outro').value);
  if(!Number.isInteger(value)||value<0||value>5000)throw Error('最後の余韻は0〜5000ミリ秒で入力してください');return value}
function payload(){return {script:$('script').value,character:character,
  caption:$('caption').value,outro_ms:outroValue(),run_asr:$('asr').checked}}
let previewTimer=null;
let previewVersion=0;
function schedulePreview(){previewVersion++;clearTimeout(previewTimer);previewTimer=setTimeout(runPreview,450)}
async function runPreview(){const box=$('preview');
  const version=previewVersion;
  if(!$('script').value.trim()){box.hidden=true;return}
  try{const j=await api('/api/segment',{script:$('script').value});
    if(version!==previewVersion)return;
    box.hidden=false;
    box.innerHTML='<p class="muted">この区切りで生成します（'+j.parts.length+'パート）</p><ol>'+
      j.parts.map((p,i)=>`<li>${esc(p.text)}${i<j.parts.length-1?`<span>次まで ${p.gap_after_ms}ms</span>`:''}</li>`).join('')+'</ol>'}
  catch(e){if(version!==previewVersion)return;box.hidden=false;box.innerHTML='<p class="muted">'+esc(e.message)+'</p>'}}

/* ---- step 3: review ---- */
function partHtml(p,i,total,jobId){
  const asr=p.asr||{}, label=ASR_LABEL[asr.status]||['','',asr.status||''];
  const issues=(p.quality&&p.quality.issues||[]).filter(v=>!/^ASR result requires/.test(v));
  const seed=Number(p.seed!=null?p.seed:(p.used_seed!=null?p.used_seed:0));
  const initial=Number(p.initial_seed!=null?p.initial_seed:seed);
  return `<div class="part">
  <div class="part-head"><span class="part-no">${p.index}</span><span>${esc(p.text)}</span>
  ${label[1]?`<span class="pill ${label[0]}" style="margin-left:auto"><span class="dot"></span>${esc(label[1])}</span>`:''}</div>
  <audio controls preload="none" src="/media/${encodeURIComponent(jobId)}/${esc(p.audio)}?v=${p.sha256?p.sha256.slice(0,12):Date.now()}"></audio>
  ${asr.transcript?`<p class="muted">音声認識: ${esc(asr.transcript)}</p>`:''}
  ${label[2]?`<p class="notice ${label[0]}" style="margin:8px 0 0">${esc(label[2])}</p>`:''}
  ${issues.map(v=>`<p class="notice warn" style="margin:8px 0 0">${esc(issueText(v))}</p>`).join('')}
  <div class="tools">
    ${i<total-1?`<label>次の文まで <input class="p-gap" data-i="${i}" type="number" min="0" max="3000" step="10" value="${p.gap_after_ms}"> ms</label>`:'<span class="muted">最後のパートです</span>'}
  </div>
  <details class="redo"><summary>このパートだけ作り直す</summary>
  <div class="tools">
    <label>言い方(seed) <input class="p-seed" data-part="${p.index}" type="number" min="0" max="4294967295" step="1" value="${seed}"></label>
    <button class="small p-seed-go" data-part="${p.index}">このseedで作り直す</button>
    <button class="small p-seed-reset" data-part="${p.index}" data-initial="${initial}"${seed===initial?' disabled data-keep="1"':''}>最初のseedに戻す</button>
  </div>
  <div class="tools">
    <label>長さ <input class="p-scale" data-part="${p.index}" type="number" min="0.1" max="4" step="0.05" value="${Number(p.duration_scale||1).toFixed(2)}"></label>
    <button class="small p-scale-go" data-part="${p.index}">この長さで作り直す</button>
    <span class="muted">1.00が標準。0.8で約20%短く、1.2で約20%長く。</span>
  </div></details></div>`}

function renderResult(m){
  current=m.job_id;$('result').className='';
  if(!m.output||!['review','completed'].includes(m.status)){
    $('result').innerHTML=`<p class="notice ${m.status==='failed'?'err':''}">${m.status==='failed'?'音声の生成が完了しませんでした。台本を確認して、もう一度「音声を作る」を押してください。':'音声を生成中です。しばらく待ってから開き直してください。'}</p>`;
    return}
  const total=m.parts.length;
  const resultCharacter=m.character_label||((defaults[m.character]||{}).label_ja)||m.character||'';
  const flagged=m.parts.filter(p=>(p.quality&&p.quality.status==='review')||
    (p.asr&&p.asr.status&&p.asr.status!=='pass'&&p.asr.status!=='not_run')).length;
  $('result').innerHTML=`
  <h3>できあがり（全体）${resultCharacter?' — '+esc(resultCharacter):''}</h3>
  <audio controls preload="metadata" src="/media/${encodeURIComponent(m.job_id)}/narration.wav?v=${m.output&&m.output.sha256?m.output.sha256.slice(0,12):Date.now()}"></audio>
  <p class="muted">${total}パート${m.output&&m.output.wav?' / '+m.output.wav.duration_seconds.toFixed(1)+'秒':''}</p>
  ${flagged?`<p class="notice warn">${flagged}件のパートを聴いて確認してください。</p>`:''}
  ${m.asr&&m.asr.status==='error'&&!m.parts.some(p=>p.asr&&p.asr.status==='error')?'<p class="notice warn">音声認識での照合に失敗しました。音声を聴いて確認してください。</p>':''}
  <h3>パートごとに聴く</h3>
  ${m.parts.map((p,i)=>partHtml(p,i,total,m.job_id)).join('')}
  <div class="tools"><button id="rejoin">間の変更を反映する</button>
    <button class="danger" id="delete-job">この音声を削除する</button></div>
  <div id="delete-job-confirm"></div>
  <div class="adopt">
    <label class="check"><input id="listened" type="checkbox" data-keep="1" ${m.human_review==='passed'?'checked':''}>
    <span>各パートと完成音声を聴いて確認しました</span></label>
    <button id="adopt" class="primary big" disabled>この音声を動画の入力にする</button>
    <div id="adopted"></div>
  </div>`;
  $('rejoin').onclick=()=>rejoin().catch(e=>flash('err',e.message));
  $('delete-job').onclick=confirmDeleteJob;
  $('adopt').onclick=()=>adopt().catch(e=>flash('err',e.message));
  $('listened').onchange=syncAdopt;syncAdopt();
  document.querySelectorAll('.p-seed-go').forEach(b=>b.onclick=()=>{
    const v=Number(document.querySelector('.p-seed[data-part="'+b.dataset.part+'"]').value);
    if(!Number.isSafeInteger(v)||v<0||v>4294967295)return flash('err','seedは0〜4294967295の整数で入力してください');
    regenerate({part_index:Number(b.dataset.part),seed:v}).catch(e=>flash('err',e.message))});
  document.querySelectorAll('.p-seed-reset').forEach(b=>b.onclick=()=>
    regenerate({part_index:Number(b.dataset.part),seed:Number(b.dataset.initial)}).catch(e=>flash('err',e.message)));
  document.querySelectorAll('.p-scale-go').forEach(b=>b.onclick=()=>{
    const v=Number(document.querySelector('.p-scale[data-part="'+b.dataset.part+'"]').value);
    if(!Number.isFinite(v)||v<0.1||v>4)return flash('err','長さは0.1〜4.0で入力してください');
    regenerate({part_index:Number(b.dataset.part),duration_scale:v}).catch(e=>flash('err',e.message))});
  if(location.hash!=='#job='+m.job_id)history.replaceState(null,'','#job='+m.job_id);
  syncControls();
}
function say(text){$('result').insertAdjacentHTML('afterbegin',
  '<p class="notice" id="working"><span class="spin"></span>'+esc(text)+'</p>')}
function working(text){const el=$('working');
  if(el)el.innerHTML='<span class="spin"></span>'+esc(text);else say(text)}

async function generate(){
  if(!$('script').value.trim())return flash('err','先に台本を入力してください');
  const data=payload();
  flash('');
  await work(null,async()=>{
    $('result').innerHTML='';say('準備を確認しています…');
    await ensureReady(working);
    working('音声を作っています…');
    const t=await api('/api/generate',data);
    const m=await poll(t.task_id,working);
    renderResult(m);loadRecent();
    $('card-review').scrollIntoView({behavior:'smooth',block:'start'})})}
async function regenerate(body){
  await work(null,async()=>{working('パートを作り直しています…');
    await ensureReady(working);
    const t=await api('/api/jobs/'+encodeURIComponent(current)+'/regenerate',
      Object.assign({run_asr:$('asr').checked},body));
    renderResult(await poll(t.task_id,working));await loadRecent()})}
async function rejoin(){
  await work(null,async()=>{working('間を反映しています…');
    const gaps=[...document.querySelectorAll('.p-gap')].map(e=>Number(e.value));
    if(gaps.some(v=>!Number.isInteger(v)||v<0||v>3000))throw Error('文の間は0〜3000ミリ秒で入力してください');
    renderResult(await api('/api/jobs/'+encodeURIComponent(current)+'/join',
      {gaps_ms:gaps,outro_ms:outroValue()}));await loadRecent()})}
async function adopt(){
  await work(null,async()=>{
    const j=await api('/api/jobs/'+encodeURIComponent(current)+'/adopt',{confirmed_listened:true});
    $('adopted').innerHTML='<p class="notice ok">入力セット <b>'+esc(j.input_set)+
      '</b> を作りました。動画生成メニューの「動画を生成」、または <code>./bin/narration-video-gen run</code> で選べます。</p>'});
  await loadRecent()}
function confirmDeleteJob(){
  $('delete-job-confirm').innerHTML=`<div class="maker">
    <p>この作成済み音声とパートを削除します。動画用にコピー済みの入力セットは残ります。</p>
    <div class="tools"><button class="danger" id="delete-job-yes">削除する</button>
    <button id="delete-job-no">やめる</button></div></div>`;
  $('delete-job-no').onclick=()=>{$('delete-job-confirm').innerHTML=''};
  $('delete-job-yes').onclick=()=>deleteJob().catch(e=>flash('err',e.message))}
async function deleteJob(){
  const deleting=current;
  await work(null,async()=>{
    await api('/api/jobs/'+encodeURIComponent(deleting)+'/delete',{});
    current=null;$('result').innerHTML='';
    history.replaceState(null,'',location.pathname);
    await loadRecent();
    flash('ok','作成済み音声を削除しました。')})}

/* ---- recent jobs ---- */
function renderSavedLists(){
  if(!savedListsLoaded)return;
  const selected=defaults[character]||{};
  const name=selected.label_ja||character;
  const inputs=libraryInputs.filter(item=>item.character===character);
  const jobs=recentJobs.filter(item=>item.character===character);
  $('card-library').hidden=false;$('library-character').textContent=name;
  $('library').innerHTML=inputs.length?inputs.map(item=>{
    const duration=Number.isFinite(Number(item.duration_seconds))
      ?Number(item.duration_seconds).toFixed(1)+'秒':'';
    const parsedDate=item.adopted_at?new Date(item.adopted_at):null;
    const date=parsedDate&&!Number.isNaN(parsedDate.getTime())
      ?parsedDate.toLocaleString('ja-JP'):'';
    const preview=String(item.script||'').split(/\r?\n/).filter(line=>line.trim()).slice(0,3).join('\n');
    return `<div class="library-item"><div class="library-head">
      <strong>${esc(item.title||item.input_set)}</strong>
      <span class="library-meta">${esc([date,duration].filter(Boolean).join(' / '))}</span></div>
      <p class="muted" style="margin-bottom:0">台本</p><pre>${esc(preview)}</pre>
      <audio controls preload="none" src="/input-media/${encodeURIComponent(item.input_set)}/audio"></audio>
      <p class="muted">runの選択名: <code>inputs/${esc(item.input_set)}</code></p>
      <div class="tools">${item.source_job_exists?`<button type="button" data-source-job="${esc(item.source_job)}">編集元を開く</button>`:''}
        <button type="button" class="danger" data-delete-input="${esc(item.input_set)}">この動画用台本を削除</button></div>
      <div class="input-delete-confirm"></div>
      <details><summary>台本全文</summary><pre>${esc(item.script||'')}</pre></details></div>`}).join('')
    :`<p class="notice">${esc(name)}で、まだ動画に使える音声・台本は作られていません。</p>`;
  document.querySelectorAll('[data-source-job]').forEach(b=>b.onclick=()=>openJob(b.dataset.sourceJob));
  document.querySelectorAll('[data-delete-input]').forEach(b=>b.onclick=()=>confirmDeleteInput(b));
  $('card-recent').hidden=false;$('recent-character').textContent=name;
  $('recent').innerHTML=jobs.length?jobs.map(item=>
    `<div class="library-item"><div class="library-head"><strong>${esc(item.title||item.job_id)}</strong>
     <span class="when">${esc(item.label||'')}</span></div>
     <p class="muted" style="margin-bottom:0">台本</p><pre>${esc(item.script_preview||item.title||'')}</pre>
     <div class="tools"><button type="button" data-job="${esc(item.job_id)}">開く</button></div></div>`).join('')
    :`<p class="notice">${esc(name)}の作成履歴はありません。</p>`;
  document.querySelectorAll('#recent button').forEach(b=>b.onclick=()=>openJob(b.dataset.job));
  syncControls()}
function confirmDeleteInput(button){
  const name=button.dataset.deleteInput;
  const box=button.closest('.library-item').querySelector('.input-delete-confirm');
  box.innerHTML=`<div class="notice warn"><p><b>inputs/${esc(name)}</b> を動画生成の選択肢から外します。
    元の作成履歴は残ります。この台本で動画を生成中なら、完了してから削除してください。</p><div class="tools">
    <button type="button" class="danger input-delete-yes">削除する</button>
    <button type="button" class="input-delete-no">やめる</button></div></div>`;
  box.querySelector('.input-delete-no').onclick=()=>{box.innerHTML=''};
  box.querySelector('.input-delete-yes').onclick=()=>deleteInputSet(name).catch(e=>flash('err',e.message))}
async function deleteInputSet(name){
  await work(null,async()=>{
    await api('/api/input-sets/'+encodeURIComponent(name)+'/delete',{});
    await loadRecent();flash('ok','動画用の音声・台本を削除しました。')})}
async function loadRecent(){
  try{const j=await api('/api/jobs');
    libraryInputs=j.input_sets||[];recentJobs=j.jobs||[];savedListsLoaded=true;
    renderSavedLists()}
  catch(e){savedListsLoaded=false;$('card-recent').hidden=true;$('card-library').hidden=true}}
async function openJob(id){try{closeMaker();await work(null,async()=>{
  const m=await api('/api/jobs/'+encodeURIComponent(id));
  $('script').value=m.script||'';$('caption').value=m.caption||'';
  $('outro').value=m.outro_ms??1500;
  if(defaults[m.character]){character=m.character;renderCharacters()}
  schedulePreview();renderResult(m);
  $('card-review').scrollIntoView({behavior:'smooth',block:'start'})})}
  catch(e){flash('err',e.message)}}

/* ---- boot ---- */
renderCharacters();$('caption').value=defaults[character].caption;
$('script').addEventListener('input',schedulePreview);
$('generate').onclick=()=>generate().catch(e=>{flash('err',e.message);const el=$('working');if(el)el.remove()});
refresh().catch(e=>flash('err',e.message));
loadRecent();
if(location.hash==='#new')openMaker();
else if(location.hash.startsWith('#char=')){
  const want=decodeURIComponent(location.hash.slice(6));
  if(defaults[want]){character=want;renderCharacters();$('caption').value=defaults[want].caption}}
else if(location.hash.startsWith('#job='))openJob(decodeURIComponent(location.hash.slice(5)));
</script></body></html>"""


def _script_json(value):
    """Embed user labels in script data without allowing an HTML end tag."""
    return json.dumps(value, ensure_ascii=False).replace("&", "\\u0026") \
        .replace("<", "\\u003c").replace(">", "\\u003e")


def _outro_ms(data):
    value = data.get("outro_ms", narration.DEFAULT_OUTRO_MS)
    if not isinstance(value, int) or not 0 <= value <= 5000:
        raise ValueError("最後の余韻は0〜5000ミリ秒で入力してください")
    return value


def _public_error(exc):
    message = str(exc)
    translations = (
        ("script is empty", "台本を入力してください"),
        ("cannot reach the local Irodori-TTS", "音声エンジンに接続できません。準備欄から起動してください"),
        ("Irodori-TTS returned HTTP", "音声の生成に失敗しました。台本を短く分けて、もう一度試してください"),
        ("Irodori-TTS response was not a WAV", "音声エンジンから音声を受け取れませんでした"),
        ("TTS models are not prepared", "準備欄からモデルをダウンロードしてください"),
        ("unknown character:", "キャラクターが見つかりません。選び直してください"),
        ("input set already exists:", "この音声の入力セットは作成済みです。動画生成で選べます"),
        ("cannot read narration job", "音声が見つかりません。作り直してください"),
        ("narration part", "パートの音声が見つかりません。ナレーションを作り直してください"),
        ("only a completed narration", "音声の生成が完了してから操作してください"),
        ("narration generation has not completed", "音声の生成が完了してから操作してください"),
        ("listening review was not confirmed", "音声を聴いて確認欄にチェックを入れてください"),
        ("listen to every part", "音声を聴いて確認欄にチェックを入れてください"),
        ("seed must be", "seedは0〜4294967295の整数で入力してください"),
        ("duration scale must be", "長さは0.1〜4.0で入力してください"),
        ("expected", "パート数と文の間の設定が合いません。音声を開き直してください"),
        ("request is too large", "台本が長すぎます。短く分けてください"),
    )
    for prefix, translated in translations:
        if message.startswith(prefix):
            return translated
    if re.search(r"[ぁ-んァ-ン一-龯]", message):
        return message
    frames = traceback.extract_tb(exc.__traceback__)
    if frames:
        origin = frames[-1]
        print("tts-web: %s at %s:%d" % (
            type(exc).__name__, Path(origin.filename).name, origin.lineno), file=sys.stderr)
    if isinstance(exc, (ValueError, TypeError, KeyError)):
        return "入力内容を確認して、もう一度試してください"
    if isinstance(exc, FileNotFoundError):
        return "必要なファイルが見つかりません。音声や画像を選び直してください"
    return "処理に失敗しました。画面を再読み込みして、もう一度試してください"


def _upload_root(root):
    return Path(root) / "outputs" / "tts" / ".uploads"


def store_upload(root, kind, filename, payload):
    """Keep one uploaded file out of the way until a character claims it."""
    if kind not in ("audio", "image"):
        raise tts_service.TTSError("音声または画像を選んでください")
    allowed = AUDIO_SUFFIXES if kind == "audio" else IMAGE_SUFFIXES
    suffix = Path(filename or "").suffix.lower()
    if suffix not in allowed:
        raise tts_service.TTSError(
            "この形式は読み込めません（%s）" % (suffix or "拡張子なし"))
    if not payload:
        raise tts_service.TTSError("ファイルが空です")
    base = _upload_root(root)
    base.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - UPLOAD_KEEP_SECONDS
    for stale in base.iterdir():
        try:
            if stale.is_dir() and stale.stat().st_mtime < cutoff:
                shutil.rmtree(stale, ignore_errors=True)
        except OSError:
            continue
    upload_id = secrets.token_hex(8)
    directory = base / upload_id
    directory.mkdir()
    (directory / ("upload" + suffix)).write_bytes(payload)
    return upload_id


def resolve_upload(root, upload_id):
    if not upload_id or not all(ch in "0123456789abcdef" for ch in upload_id):
        raise tts_service.TTSError("アップロードが見つかりません")
    directory = _upload_root(root) / upload_id
    files = sorted(directory.glob("upload.*")) if directory.is_dir() else []
    if not files:
        raise tts_service.TTSError("アップロードが見つかりません。選び直してください")
    return files[0]


class TaskStore:
    def __init__(self):
        self.values = {}
        self.active = {}
        self.lock = threading.Lock()

    def start(self, label, function, key=None):
        task_id = secrets.token_hex(8)
        with self.lock:
            if key:
                existing = self.active.get(key)
                if existing and self.values.get(existing, {}).get("status") == "running":
                    return existing
                self.active.pop(key, None)
            # A long-lived page polls many tasks; keep the newest few results
            # instead of holding every manifest for the life of the process.
            while len(self.values) >= MAX_TASKS:
                for key, value in list(self.values.items()):
                    if value.get("status") != "running":
                        del self.values[key]
                        break
                else:
                    raise tts_service.TTSError(
                        "同時に実行中の処理が多すぎます。完了を待ってから再試行して")
            self.values[task_id] = {"status": "running", "message": label}
            if key:
                self.active[key] = task_id

        def run():
            try:
                result = function(lambda message: self.update(task_id, message))
                with self.lock:
                    self.values[task_id] = {
                        "status": "completed", "message": "完了", "result": result}
            except Exception as exc:
                with self.lock:
                    self.values[task_id] = {
                        "status": "failed", "message": "失敗", "error": _public_error(exc)}
            finally:
                with self.lock:
                    if key and self.active.get(key) == task_id:
                        del self.active[key]
        threading.Thread(target=run, daemon=True).start()
        return task_id

    def update(self, task_id, message):
        with self.lock:
            if task_id in self.values:
                self.values[task_id]["message"] = message

    def get(self, task_id):
        with self.lock:
            return dict(self.values.get(task_id) or {})


def video_input_sets(root):
    """List adopted TTS inputs that are selectable by the video runner."""
    root = Path(root)
    raw_base = root / "inputs"
    if raw_base.is_symlink() or not raw_base.is_dir():
        return []
    base = raw_base.resolve()
    if os.path.commonpath([str(root.resolve()), str(base)]) != str(root.resolve()):
        return []
    characters = tts_service.list_characters(root)
    listed = []
    for directory in base.iterdir():
        try:
            if directory.is_symlink() or not directory.is_dir():
                continue
            directory = directory.resolve()
            if os.path.commonpath([str(base), str(directory)]) != str(base):
                continue
            manifest_path = directory / "tts-manifest.json"
            script_path = directory / "script.txt"
            audio = directory / "audio.wav"
            if any(path.is_symlink() for path in (
                    manifest_path, script_path, audio)):
                continue
            if not all(path.is_file() for path in (
                    manifest_path, script_path, audio)):
                continue
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict):
                continue
            script = script_path.read_text(encoding="utf-8")
            wavs = [path for path in directory.rglob("*")
                    if path.is_file() and path.suffix.lower() == ".wav"]
            images = [path for path in directory.rglob("*")
                      if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES]
            if len(wavs) != 1 or len(images) != 1:
                continue
            if any(path.is_symlink() for path in wavs + images):
                continue
            if any(os.path.commonpath([str(directory), str(path.resolve())])
                   != str(directory) for path in wavs + images):
                continue
            if wavs[0].resolve() != audio.resolve():
                continue
            with wave.open(str(audio), "rb") as source_wav:
                rate = source_wav.getframerate()
                measured_duration = (source_wav.getnframes() / float(rate)
                                     if rate else 0)
            if measured_duration <= 0:
                continue
            source_job = manifest.get("source_job")
            source_job = source_job if isinstance(source_job, str) else None
            source = None
            if source_job:
                try:
                    loaded = tts_service.load_job(root, source_job)
                    source = loaded if isinstance(loaded, dict) else None
                except tts_service.TTSError:
                    pass
            character_id = manifest.get("character")
            character_id = character_id if isinstance(character_id, str) else ""
            character = characters.get(character_id, {})
            first_line = next((line.strip() for line in script.splitlines()
                               if line.strip()), "")
            duration = manifest.get("duration_seconds")
            if not isinstance(duration, (int, float)) or duration <= 0:
                duration = (source or {}).get("output", {}).get(
                    "wav", {}).get("duration_seconds") or measured_duration
            adopted_at = manifest.get("adopted_at")
            if not isinstance(adopted_at, str) or not adopted_at:
                adopted_at = time.strftime(
                    "%Y-%m-%dT%H:%M:%S%z",
                    time.localtime(manifest_path.stat().st_mtime))
            title = manifest.get("title")
            title = title if isinstance(title, str) else ""
            character_label = manifest.get("character_label")
            character_label = character_label if isinstance(
                character_label, str) else ""
            listed.append({
                "input_set": directory.name,
                "source_job": source_job,
                "source_job_exists": source is not None,
                "character": character_id,
                "character_label": character_label
                    or (source or {}).get("character_label")
                    or character.get("label_ja") or character_id,
                "title": title or first_line[:80] or directory.name,
                "script": script,
                "duration_seconds": duration,
                "adopted_at": adopted_at,
            })
        except (OSError, ValueError, TypeError, wave.Error):
            # One manually edited or concurrently removed set must not hide
            # every other usable narration and the independent job history.
            continue
    return sorted(listed, key=lambda item: (
        item.get("adopted_at") or "", item["input_set"]), reverse=True)


def recent_jobs(root, limit=RECENT_JOB_LIMIT, input_sets=None):
    """List the newest narration jobs so a closed tab can be resumed."""
    base = Path(root) / "outputs" / "tts"
    if not base.is_dir():
        return []
    input_sets = video_input_sets(root) if input_sets is None else input_sets
    adopted_by_job = {}
    for item in input_sets:
        if item.get("source_job"):
            adopted_by_job.setdefault(item["source_job"], []).append(
                item["input_set"])
    listed = []
    per_character = {}
    characters = tts_service.list_characters(root)
    for directory in sorted((path for path in base.iterdir() if path.is_dir()),
                            reverse=True):
        try:
            manifest = json.loads(
                (directory / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(manifest, dict):
            continue
        if manifest.get("purpose") == "voice_test":
            continue
        character_id = manifest.get("character", "")
        if per_character.get(character_id, 0) >= limit:
            continue
        parts = manifest.get("parts") or []
        script = manifest.get("script", "")
        script = script if isinstance(script, str) else ""
        title = (parts[0].get("text") if parts else "") or script
        character = characters.get(manifest.get("character"), {})
        job_id = manifest.get("job_id", directory.name)
        adopted = adopted_by_job.get(job_id, [])
        status = manifest.get("status")
        if adopted:
            state = "動画用に保存済み: %s" % ", ".join(adopted)
        elif status == "failed":
            state = "生成失敗"
        elif status == "generating":
            state = "生成中"
        elif manifest.get("human_review") == "passed":
            state = "確認済み・動画用には未保存"
        else:
            state = "未確認"
        listed.append({
            "job_id": job_id,
            "character": character_id,
            "title": title[:40],
            "script_preview": "\n".join([
                line.strip() for line in script.splitlines()
                if line.strip()][:3])[:240],
            "label": "%s / %sパート / %s" % (
                manifest.get("character_label")
                or character.get("label_ja", manifest.get("character", "")),
                len(parts), state),
            "state": state,
            "adopted_input_sets": adopted,
        })
        per_character[character_id] = per_character.get(character_id, 0) + 1
    return listed


class NarrationWebApp:
    def __init__(self, root, backend=tts_service.DEFAULT_SERVER,
                 allowed_hosts=(), auth_required=False):
        self.root = Path(root).resolve()
        self.backend = backend
        self.csrf = secrets.token_urlsafe(24)
        self.tasks = TaskStore()
        self.allowed_hosts = tuple(allowed_hosts)
        self.auth_required = auth_required
        self.auth_sessions = {}
        self.auth_failures = {}
        self.auth_lock = threading.Lock()
        self.auth_verify_slots = threading.BoundedSemaphore(2)
        self.active_character_drafts = set()
        self.character_draft_lock = threading.Lock()

    def draft_busy(self, character_id):
        with self.character_draft_lock:
            return character_id in self.active_character_drafts

    def login(self, password, peer):
        """Return a new browser-session token for the fixed TTS user."""
        now = time.monotonic()
        with self.auth_lock:
            failures = [item for item in self.auth_failures.get(peer, ())
                        if now - item < AUTH_FAILURE_WINDOW_SECONDS]
            self.auth_failures[peer] = failures
            if len(failures) >= AUTH_FAILURE_LIMIT:
                return None, True
        if not self.auth_verify_slots.acquire(blocking=False):
            return None, True
        try:
            record = password_configured(self.root)
            matched = _password_matches_record(record, AUTH_USER, password)
        finally:
            self.auth_verify_slots.release()
        if not matched:
            with self.auth_lock:
                self.auth_failures.setdefault(peer, []).append(now)
            return None, False
        current = password_configured(self.root)
        if (not record or not current or not hmac.compare_digest(
                str(record.get("hash", "")), str(current.get("hash", "")))):
            return None, False
        token = secrets.token_urlsafe(32)
        with self.auth_lock:
            self.auth_failures.pop(peer, None)
            while len(self.auth_sessions) >= MAX_AUTH_SESSIONS:
                self.auth_sessions.pop(next(iter(self.auth_sessions)))
            self.auth_sessions[token] = (
                record.get("hash"), time.monotonic() + AUTH_SESSION_SECONDS)
        return token, False

    def session_valid(self, token):
        if not token:
            return False
        record = password_configured(self.root)
        with self.auth_lock:
            session = self.auth_sessions.get(token)
            fingerprint, expires = session if session else (None, 0)
            valid = bool(record and fingerprint and expires > time.monotonic()
                         and hmac.compare_digest(
                             str(fingerprint), str(record.get("hash", ""))))
            if not valid:
                self.auth_sessions.pop(token, None)
        return valid

    def handler(self):
        app = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "NarrationReview/1"

            def log_message(self, format_string, *args):
                print("tts-web: " + format_string % args)

            def log_request(self, code="-", size="-"):
                # Polling a generation job should not fill the terminal or log.
                if isinstance(code, int) and code >= 500:
                    self.log_message("HTTP %s", code)

            def _send(self, status, content_type, body, extra=()):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                for name, value in extra:
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(body)

            def _redirect(self, location, extra=()):
                self._send(303, "text/plain; charset=utf-8", b"", extra=(
                    ("Location", location), *extra))

            def _json(self, status, payload):
                self._send(status, "application/json; charset=utf-8",
                           json.dumps(payload, ensure_ascii=False).encode("utf-8"))

            def _raw_body(self):
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    raise ValueError("invalid request length")
                if length < 0:
                    raise ValueError("invalid request length")
                if length > MAX_UPLOAD_BYTES:
                    raise ValueError("ファイルが大きすぎます（上限64MB）")
                return self.rfile.read(length)

            def _body(self):
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    raise ValueError("invalid request length")
                if length < 0:
                    raise ValueError("invalid request length")
                if length > 1024 * 1024:
                    raise ValueError("request is too large")
                data = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(data, dict):
                    raise ValueError("invalid request body")
                return data

            def _csrf_ok(self):
                return secrets.compare_digest(
                    self.headers.get("X-NVG-CSRF", ""), app.csrf)

            def _host_ok(self):
                if host_allowed(self.headers.get("Host"), app.allowed_hosts):
                    return True
                self._json(403, {"error": "unexpected Host header"})
                return False

            def _auth_token(self):
                try:
                    cookie = SimpleCookie()
                    cookie.load(self.headers.get("Cookie", ""))
                    value = cookie.get(AUTH_COOKIE)
                    return value.value if value else ""
                except CookieError:
                    return ""

            def _auth_ok(self):
                if not app.auth_required:
                    return True
                return app.session_valid(self._auth_token())

            def _login_page(self, status=200, failed=False, throttled=False):
                if throttled:
                    error = '<p class="error">試行回数が多すぎます。1分待ってから試してください。</p>'
                else:
                    error = ('<p class="error">パスワードが違います。</p>'
                             if failed else "")
                self._send(status, "text/html; charset=utf-8",
                           _LOGIN_HTML.replace("__ERROR__", error).encode("utf-8"))

            def _unauthorized(self, page=False):
                if page:
                    self._redirect("/login")
                else:
                    self._json(401, {"error": "ログインしてください"})

            def do_GET(self):
                if not self._host_ok():
                    return
                parsed = urllib.parse.urlsplit(self.path)
                if parsed.path == "/login":
                    if self._auth_ok():
                        self._redirect("/")
                    else:
                        self._login_page()
                    return
                if not self._auth_ok():
                    self._unauthorized(page=parsed.path == "/")
                    return
                try:
                    if parsed.path == "/":
                        defaults = _script_json(tts_service.list_characters(app.root))
                        body = _HTML.replace("__CSRF__", json.dumps(app.csrf)) \
                                    .replace("__CAPTION__", _script_json(
                                        tts_service.DEFAULT_USER_CAPTION)) \
                                    .replace("__DESIGN__", _script_json(
                                        tts_service.DEFAULT_DESIGN_CAPTION)) \
                                    .replace("__DEFAULTS__", defaults).encode("utf-8")
                        self._send(200, "text/html; charset=utf-8", body)
                    elif parsed.path == "/api/config":
                        all_characters = tts_service.list_characters(
                            app.root, include_drafts=True)
                        self._json(200, {
                            "model_prepared": tts_service.models_prepared(app.root),
                            "backend": tts_service.backend_health(app.backend),
                            "characters": tts_service.list_characters(app.root),
                            "pending_characters": {
                                key: dict(value, busy=app.draft_busy(key))
                                for key, value in all_characters.items()
                                if value.get("draft") is True},
                            "default_caption": tts_service.DEFAULT_USER_CAPTION,
                            "design_caption": tts_service.DEFAULT_DESIGN_CAPTION,
                        })
                    elif parsed.path.startswith("/api/tasks/"):
                        task = app.tasks.get(parsed.path.rsplit("/", 1)[-1])
                        self._json(200 if task else 404, task or {
                            "error": "処理が見つかりません。画面を再読み込みしてください"})
                    elif parsed.path == "/api/jobs":
                        input_sets = video_input_sets(app.root)
                        self._json(200, {
                            "jobs": recent_jobs(app.root, input_sets=input_sets),
                            "input_sets": input_sets,
                        })
                    elif parsed.path.startswith("/api/jobs/"):
                        job_id = parsed.path.rsplit("/", 1)[-1]
                        self._json(200, tts_service.load_job(app.root, job_id))
                    elif parsed.path.startswith("/character/"):
                        self._character_file(parsed.path)
                    elif parsed.path.startswith("/media/"):
                        self._media(parsed.path)
                    elif parsed.path.startswith("/input-media/"):
                        self._input_media(parsed.path)
                    else:
                        self._json(404, {"error": "not found"})
                except Exception as exc:
                    self._json(400, {"error": _public_error(exc)})

            def _character_file(self, request_path):
                """Serve only the portrait or reference a character declares."""
                parts = request_path.split("/")
                if len(parts) != 4 or parts[3] not in ("portrait", "reference"):
                    raise ValueError("not found")
                entry = tts_service.list_characters(
                    app.root, include_drafts=True).get(
                    urllib.parse.unquote(parts[2]))
                if not entry:
                    raise ValueError("not found")
                target = (app.root / entry[
                    "portrait" if parts[3] == "portrait" else "reference"]).resolve()
                if os.path.commonpath([str(app.root), str(target)]) != str(app.root) \
                        or not target.is_file():
                    raise ValueError("not found")
                body = target.read_bytes()
                self._send(200, mimetypes.guess_type(target.name)[0]
                           or "application/octet-stream", body,
                           extra=(("Accept-Ranges", "none"),))

            def _media(self, request_path):
                parts = request_path.split("/", 3)
                if len(parts) != 4:
                    raise ValueError("invalid media path")
                job_id = urllib.parse.unquote(parts[2])
                relative = urllib.parse.unquote(parts[3])
                base = tts_service.job_root(app.root, job_id).resolve()
                target = (base / relative).resolve()
                if os.path.commonpath([str(base), str(target)]) != str(base) \
                        or not target.is_file() or target.suffix.lower() != ".wav":
                    raise ValueError("media not found")
                content_type = (mimetypes.guess_type(target.name)[0]
                                or "application/octet-stream")
                body = target.read_bytes()
                # Range keeps the browser's seek bar usable on long narrations.
                first, last = self._range(len(body))
                if first is None:
                    self._send(200, content_type, body,
                               extra=(("Accept-Ranges", "bytes"),))
                    return
                self._send(206, content_type, body[first:last + 1], extra=(
                    ("Accept-Ranges", "bytes"),
                    ("Content-Range", "bytes %d-%d/%d" % (first, last, len(body)))))

            def _input_media(self, request_path):
                parts = request_path.split("/")
                if len(parts) != 4 or parts[3] != "audio":
                    raise ValueError("invalid input media path")
                input_set = urllib.parse.unquote(parts[2])
                if not input_set or Path(input_set).name != input_set:
                    raise ValueError("invalid input set")
                raw_inputs_root = app.root / "inputs"
                raw_base = raw_inputs_root / input_set
                if raw_inputs_root.is_symlink() or raw_base.is_symlink():
                    raise ValueError("input audio not found")
                base = raw_base.resolve()
                inputs_root = raw_inputs_root.resolve()
                raw_target = base / "audio.wav"
                manifest = base / "tts-manifest.json"
                if raw_target.is_symlink() or manifest.is_symlink():
                    raise ValueError("input audio not found")
                target = raw_target.resolve()
                if (os.path.commonpath([str(inputs_root), str(base)])
                        != str(inputs_root)
                        or os.path.commonpath([str(base), str(target)]) != str(base)
                        or not manifest.is_file()
                        or not target.is_file()):
                    raise ValueError("input audio not found")
                body = target.read_bytes()
                first, last = self._range(len(body))
                if first is None:
                    self._send(200, "audio/x-wav", body,
                               extra=(("Accept-Ranges", "bytes"),))
                    return
                self._send(206, "audio/x-wav", body[first:last + 1], extra=(
                    ("Accept-Ranges", "bytes"),
                    ("Content-Range", "bytes %d-%d/%d" % (first, last, len(body)))))

            def _range(self, size):
                match = re.match(r"^bytes=(\d*)-(\d*)$",
                                 self.headers.get("Range", "").strip())
                if not match or size == 0:
                    return None, None
                start, end = match.group(1), match.group(2)
                if start:
                    first = int(start)
                    last = min(int(end), size - 1) if end else size - 1
                elif end:
                    first, last = max(0, size - int(end)), size - 1
                else:
                    return None, None
                if first > last or first >= size:
                    return None, None
                return first, last

            def do_POST(self):
                if not self._host_ok():
                    return
                parsed = urllib.parse.urlsplit(self.path)
                if parsed.path == "/login" and app.auth_required:
                    try:
                        length = int(self.headers.get("Content-Length", "0"))
                        if length < 0 or length > 4096:
                            raise ValueError("invalid login request")
                        fields = urllib.parse.parse_qs(
                            self.rfile.read(length).decode("utf-8"),
                            keep_blank_values=True, strict_parsing=True)
                        token, throttled = app.login(
                            (fields.get("password") or [""])[0], self.client_address[0])
                    except (UnicodeDecodeError, ValueError):
                        token, throttled = None, False
                    if not token:
                        self._login_page(status=429 if throttled else 401,
                                         failed=not throttled, throttled=throttled)
                        return
                    self._redirect("/", extra=((
                        "Set-Cookie", "%s=%s; Path=/; HttpOnly; SameSite=Strict"
                        % (AUTH_COOKIE, token)),))
                    return
                if not self._auth_ok():
                    self._unauthorized()
                    return
                if not self._csrf_ok():
                    self._json(403, {"error": "接続が更新されました。画面を再読み込みしてください"})
                    return
                try:
                    if parsed.path == "/api/uploads":
                        self._json(200, {"upload_id": store_upload(
                            app.root, self.headers.get("X-NVG-Kind", ""),
                            self.headers.get("X-NVG-Filename", ""),
                            self._raw_body())})
                        return
                    data = self._body()
                    if parsed.path == "/api/segment":
                        planned = narration.split_script(data.get("script", ""))
                        self._json(200, {"parts": narration.serialise_parts(planned)})
                    elif parsed.path == "/api/prepare":
                        task_id = app.tasks.start(
                            "モデルをダウンロード中", app._prepare,
                            key="prepare-models")
                        self._json(202, {"task_id": task_id})
                    elif parsed.path == "/api/backend/start":
                        task_id = app.tasks.start(
                            "音声エンジンを起動中",
                            lambda update: app._backend_start(data.get("device", "auto"), update))
                        self._json(202, {"task_id": task_id})
                    elif parsed.path == "/api/characters":
                        task_id = app.tasks.start(
                            "キャラクターを作成中",
                            lambda update: app._create_character(data, update))
                        self._json(202, {"task_id": task_id})
                    elif parsed.path.startswith("/api/characters/") \
                            and parsed.path.endswith("/voice"):
                        character_id = urllib.parse.unquote(parsed.path.split("/")[3])
                        task_id = app.tasks.start(
                            "声を作り直しています",
                            lambda update: app._redesign_voice(
                                character_id, data, update))
                        self._json(202, {"task_id": task_id})
                    elif parsed.path.startswith("/api/characters/") \
                            and parsed.path.endswith("/portrait"):
                        character_id = urllib.parse.unquote(parsed.path.split("/")[3])
                        upload = resolve_upload(app.root, data.get("image_upload"))
                        tts_service.replace_character_portrait(
                            app.root, character_id, upload)
                        shutil.rmtree(upload.parent, ignore_errors=True)
                        self._json(200, {"character": character_id})
                    elif parsed.path.startswith("/api/characters/") \
                            and parsed.path.endswith("/delete"):
                        character_id = urllib.parse.unquote(parsed.path.split("/")[3])
                        if app.draft_busy(character_id):
                            raise tts_service.TTSError(
                                "試し聞きを生成中です。完了を待ってください")
                        tts_service.delete_user_character(app.root, character_id)
                        self._json(200, {"deleted": character_id})
                    elif parsed.path.startswith("/api/characters/") \
                            and parsed.path.endswith("/commit"):
                        character_id = urllib.parse.unquote(parsed.path.split("/")[3])
                        if app.draft_busy(character_id):
                            raise tts_service.TTSError(
                                "試し聞きを生成中です。完了を待ってください")
                        tts_service.commit_user_character(app.root, character_id)
                        self._json(200, {"character": character_id})
                    elif len(parsed.path.split("/")) == 5 \
                            and parsed.path.startswith("/api/input-sets/") \
                            and parsed.path.endswith("/delete"):
                        input_set = urllib.parse.unquote(parsed.path.split("/")[3])
                        tts_service.archive_adopted_input_set(app.root, input_set)
                        self._json(200, {"deleted": input_set, "recoverable": True})
                    elif parsed.path == "/api/generate":
                        task_id = app.tasks.start(
                            "ナレーションを生成中",
                            lambda update: app._generate(data, update))
                        self._json(202, {"task_id": task_id})
                    elif parsed.path.startswith("/api/jobs/") and parsed.path.endswith("/join"):
                        job_id = urllib.parse.unquote(parsed.path.split("/")[3])
                        gaps = data["gaps_ms"]
                        if any(not isinstance(item, int) or not 0 <= item <= 3000
                               for item in gaps):
                            raise ValueError("文の間は0〜3000ミリ秒で入力してください")
                        result = tts_service.rejoin_job(
                            app.root, job_id, [int(item) for item in data["gaps_ms"]],
                            _outro_ms(data))
                        self._json(200, result)
                    elif parsed.path.startswith("/api/jobs/") and parsed.path.endswith("/adopt"):
                        job_id = urllib.parse.unquote(parsed.path.split("/")[3])
                        if data.get("confirmed_listened") is not True:
                            raise ValueError("listening review was not confirmed")
                        tts_service.confirm_human_review(app.root, job_id)
                        target = tts_service.adopt_as_input_set(app.root, job_id)
                        self._json(200, {"input_set": target.name, "path": str(target)})
                    elif parsed.path.startswith("/api/jobs/") and parsed.path.endswith("/regenerate"):
                        job_id = urllib.parse.unquote(parsed.path.split("/")[3])
                        task_id = app.tasks.start(
                            "パートを再生成中",
                            lambda update: tts_service.regenerate_part(
                                app.root, job_id, int(data["part_index"]),
                                server=app.backend,
                                duration_scale=data.get("duration_scale"),
                                seed=data.get("seed"),
                                run_asr=data.get("run_asr", True) is True,
                                on_progress=update))
                        self._json(202, {"task_id": task_id})
                    elif parsed.path.startswith("/api/jobs/") and parsed.path.endswith("/delete"):
                        job_id = urllib.parse.unquote(parsed.path.split("/")[3])
                        tts_service.delete_narration_job(app.root, job_id)
                        self._json(200, {"deleted": job_id})
                    else:
                        self._json(404, {"error": "not found"})
                except Exception as exc:
                    self._json(400, {"error": _public_error(exc)})

        return Handler

    def _script(self, *arguments):
        """Run the backend helper, reporting its message instead of a traceback."""
        command = [str(self.root / "scripts" / "tts-backend.sh")]
        command.extend(arguments)
        action = re.sub(r"[^a-zA-Z0-9_-]", "_", arguments[0])
        log_path = self.root / "outputs" / "tts" / (action + ".log")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with log_path.open("a", encoding="utf-8") as log:
                log.write("\n[%s] %s\n" % (
                    time.strftime("%Y-%m-%d %H:%M:%S%z"), " ".join(arguments)))
                log.flush()
                subprocess.run(command, cwd=str(self.root), check=True,
                               stdout=log, stderr=subprocess.STDOUT, text=True)
                log.write("[%s] completed\n" %
                          time.strftime("%Y-%m-%d %H:%M:%S%z"))
        except FileNotFoundError as exc:
            raise tts_service.TTSError(
                "scripts/tts-backend.sh が見つかりません: %s" % exc) from exc
        except subprocess.CalledProcessError as exc:
            try:
                with log_path.open("rb") as log:
                    size = log.seek(0, os.SEEK_END)
                    log.seek(max(0, size - 4096))
                    detail = " ".join(log.read().decode(
                        "utf-8", errors="replace").split())[-300:]
            except OSError:
                detail = ""
            raise tts_service.TTSError(
                "%s に失敗しました%s（ログ: %s）" % (
                    arguments[0], ("： " + detail) if detail else "",
                    log_path.relative_to(self.root))) from exc

    def _prepare(self, update):
        update("モデルをダウンロード中（約3.7GB）")
        self._script("prepare", "--device", "auto")
        return tts_service.record_models_prepared(self.root)

    def _backend_start(self, device, update):
        if not tts_service.models_prepared(self.root):
            raise tts_service.TTSError("先にモデルをダウンロードして")
        update("音声エンジンを起動中")
        self._script("start", "--device", device)
        for _attempt in range(90):
            health = tts_service.backend_health(self.backend)
            if health["reachable"]:
                return health
            time.sleep(1)
        raise tts_service.TTSError("音声エンジンが90秒以内に応答しなかった")

    def _create_character(self, data, update):
        """Import a portrait and a recording, then prove the voice out loud."""
        if not tts_service.models_prepared(self.root):
            raise tts_service.TTSError(
                "先にモデルをダウンロードしてください")
        audio = (resolve_upload(self.root, data.get("audio_upload"))
                 if data.get("audio_upload") else None)
        portrait = (resolve_upload(self.root, data.get("image_upload"))
                    if data.get("image_upload") else None)
        designed_from = None
        staged = None
        if data.get("mode") == "design":
            if not tts_service.backend_health(self.backend)["reachable"]:
                update("音声エンジンを起動しています")
                self._backend_start("auto", update)
            supplied_seed = data.get("design_seed")
            seed = int(secrets.randbelow(2 ** 32) if supplied_seed is None else supplied_seed)
            designed_from = {"caption": (data.get("caption") or "").strip(),
                             "seed": seed}
            update("書いた特徴から声を作っています")
            staged = _upload_root(self.root) / secrets.token_hex(8)
            audio = tts_service.design_reference_wav(
                self.backend, designed_from["caption"], seed,
                staged / "designed.wav", root=self.root, provenance=designed_from,
                method=data.get("design_method", "no_ref"))
        update("音声と画像を確認しています")
        try:
            created = tts_service.create_user_character(
                self.root, data.get("label"), portrait_path=portrait,
                audio_path=audio, voice_from=data.get("voice_from") or None,
                consent_confirmed=data.get("consent") is True,
                caption=data.get("caption") or None, designed_from=designed_from,
                draft=True)
            with self.character_draft_lock:
                self.active_character_drafts.add(created["id"])
        finally:
            if staged is not None:
                shutil.rmtree(staged, ignore_errors=True)
        # Uploads are deliberately left in place. They are pruned by age, and
        # consuming them here made a second attempt from the same filled-in
        # form fail with "the upload is gone" instead of making a character.
        # Measured on the pinned server commit: the engine re-reads its alias
        # file per request, so a new character is usable immediately and does
        # not pay for a restart. Only a stopped engine has to be started.
        try:
            if not tts_service.backend_health(self.backend)["reachable"]:
                update("音声エンジンを起動しています")
                self._backend_start("auto", update)
            update("試しに一言だけ作っています")
            manifest, _directory = tts_service.generate_narration(
                self.root, tts_service.VOICE_TEST_SCRIPT, created["id"],
                server=self.backend, run_asr=False,
                metadata={"purpose": "voice_test"}, allow_draft=True)
            created["test"] = manifest
            return created
        finally:
            with self.character_draft_lock:
                self.active_character_drafts.discard(created["id"])

    def _redesign_voice(self, character_id, data, update):
        if not tts_service.backend_health(self.backend)["reachable"]:
            update("音声エンジンを起動しています")
            self._backend_start("auto", update)
        update("書いた特徴から声を作り直しています")
        changed = tts_service.redesign_character_voice(
            self.root, character_id, server=self.backend,
            caption=data.get("caption") or None,
            seed=(int(data["seed"]) if data.get("seed") is not None else None))
        update("試しに一言だけ作っています")
        manifest, _directory = tts_service.generate_narration(
            self.root, tts_service.VOICE_TEST_SCRIPT, character_id,
            server=self.backend, run_asr=False,
            metadata={"purpose": "voice_test"}, allow_draft=True)
        changed["test"] = manifest
        return changed

    def _generate(self, data, update):
        def progress(index, total, text):
            update("%d/%d を生成中: %s" % (index, total, text[:30]))
        manifest, _directory = tts_service.generate_narration(
            self.root, data.get("script", ""), data.get("character", "aoi"),
            server=self.backend, caption=data.get("caption") or None,
            outro_ms=_outro_ms(data), on_progress=progress,
            run_asr=data.get("run_asr", True) is True)
        return manifest


def serve(root, host="0.0.0.0", port=7861, backend=tts_service.DEFAULT_SERVER,
          allowed_hosts=(), auth_required=False):
    app = NarrationWebApp(root, backend=backend, allowed_hosts=allowed_hosts,
                          auth_required=auth_required)
    server = ThreadingHTTPServer((host, int(port)), app.handler())
    print("Narration UI: http://%s:%d" % (
        "127.0.0.1" if host in ("0.0.0.0", "::", "") else host, int(port)))
    if host in ("0.0.0.0", "::", ""):
        print("This page is reachable from the whole LAN%s."
              % (" and requires login" if auth_required else ""))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
