"""历史页面与测试页面的 HTML。"""

BASE_CSS = """
*{box-sizing:border-box}
body{margin:0;font:14px/1.6 -apple-system,"Microsoft YaHei",sans-serif;
     background:#0f1115;color:#e6e6e6}
header{padding:10px 18px;background:#171a21;border-bottom:1px solid #2a2f3a;
       display:flex;gap:18px;align-items:center}
header b{color:#7cc4ff}
header a{color:#9aa4b2;text-decoration:none;font-size:13px;padding:4px 10px;
         border-radius:6px}
header a.on,header a:hover{background:#232833;color:#e6e6e6}
main{padding:18px}
h3{font-size:14px;color:#9aa4b2;margin:18px 0 10px;font-weight:500;
   border-left:3px solid #2f6feb;padding-left:8px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid #1e232c}
th{color:#9aa4b2;font-weight:500;background:#141821}
tr:hover td{background:#151a22}
button{padding:6px 14px;border:1px solid #333a46;border-radius:6px;
       background:#0f1115;color:#cfd6e0;cursor:pointer;font-size:13px}
button:hover{border-color:#7cc4ff}
button.primary{background:#2f6feb;border-color:#2f6feb;color:#fff}
input,select{padding:6px 10px;border-radius:6px;border:1px solid #333a46;
       background:#0f1115;color:#e6e6e6;font-size:13px}
.row{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin-bottom:14px}
pre{background:#0b0d11;border:1px solid #1e232c;border-radius:6px;padding:10px;
    overflow:auto;font:12px/1.6 Consolas,monospace;max-height:320px;color:#a8b3c0}
.kept{color:#8fd694}
.drop{color:#e06c6c}
.muted{color:#5f6875;font-size:12px}
.t{color:#5f6875;font-size:12px;margin-right:6px}
.u{color:#7cc4ff}
.pill{display:inline-block;padding:1px 7px;border-radius:9px;font-size:11px;
      background:#232833;color:#9aa4b2;margin-right:6px}
"""

NAV = """
<header>
  <b>直播情报台</b>
  <a href="/" class="{live}">实时监控</a>
  <a href="/history" class="{hist}">历史</a>
  <a href="/test" class="{test}">测试</a>
  <a href="/kb" class="{kb}">知识库</a>
</header>
"""

HISTORY = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>历史直播</title><style>__CSS__</style></head><body>
__NAV__
<main>
  <div class="row">
    <label>从 <input type="date" id="from"></label>
    <label>到 <input type="date" id="to"></label>
    <label>房间 <input type="text" id="room" placeholder="房间号" style="width:110px"></label>
    <button class="primary" onclick="load()">查询</button>
    <button onclick="setDays(7)">最近 7 天</button>
    <button onclick="setDays(30)">最近 30 天</button>
  </div>
  <h3>场次 <span class="muted" id="cnt"></span></h3>
  <table><thead><tr>
    <th>场次</th><th>房间</th><th>开始时间</th><th>时长</th>
    <th>弹幕</th><th>语音</th><th>用户</th><th></th>
  </tr></thead><tbody id="rows"></tbody></table>

  <div id="detail" style="display:none">
    <h3>详情 <span class="muted" id="dt"></span></h3>
    <div class="row">
      <input type="text" id="kw" placeholder="在内容里搜索关键词" style="width:220px">
      <select id="type">
        <option value="">全部</option><option value="danmaku">仅弹幕</option>
        <option value="speech">仅语音</option>
      </select>
      <button onclick="loadEvents()">筛选</button>
      <span class="muted" id="evcnt"></span>
    </div>
    <pre id="events"></pre>
  </div>
</main>
<script>
let cur = null;
function setDays(n){
  const to = new Date(), from = new Date(Date.now() - n*86400000);
  const f = d => d.toISOString().slice(0,10);
  document.getElementById('from').value = f(from);
  document.getElementById('to').value = f(to);
  load();
}
function load(){
  const p = new URLSearchParams();
  const f = document.getElementById('from').value;
  const t = document.getElementById('to').value;
  const r = document.getElementById('room').value;
  if(f) p.set('from', f);
  if(t) p.set('to', t);
  if(r) p.set('room', r);
  fetch('/api/sessions?'+p).then(r=>r.json()).then(list=>{
    const tb = document.getElementById('rows');
    tb.innerHTML = '';
    document.getElementById('cnt').textContent = '共 '+list.length+' 场';
    list.forEach(s=>{
      const tr = document.createElement('tr');
      tr.innerHTML = '<td>'+s.id+'</td><td>'+s.room_id+'</td><td>'+s.started+
        '</td><td>'+s.duration+'</td><td>'+s.danmaku+'</td><td>'+s.speech+
        '</td><td>'+s.users+'</td>'+
        '<td><button onclick="pick(\\''+s.id+'\\')">查看</button></td>';
      tb.appendChild(tr);
    });
    if(!list.length) tb.innerHTML = '<tr><td colspan="8" class="muted">没有符合条件的场次</td></tr>';
  });
}
function pick(id){
  cur = id;
  document.getElementById('detail').style.display='block';
  document.getElementById('dt').textContent = id;
  loadEvents();
}
function loadEvents(){
  if(!cur) return;
  const p = new URLSearchParams({session:cur, limit:'500'});
  const kw = document.getElementById('kw').value;
  const ty = document.getElementById('type').value;
  if(kw) p.set('q', kw);
  if(ty) p.set('type', ty);
  fetch('/api/events?'+p).then(r=>r.json()).then(list=>{
    document.getElementById('evcnt').textContent = '共 '+list.length+' 条';
    document.getElementById('events').innerHTML = list.map(e=>{
      const cls = e.type==='speech' ? 'kept' : '';
      const tag = e.type==='speech' ? '[主播]' : (e.is_bot? '[机器人]' : '[弹幕]');
      return '<span class="t">'+e.clock+'</span><span class="'+cls+'">'+tag+
             '</span> <span class="u">'+(e.uname||'')+'</span>: '+
             (e.text||'').replace(/[<>]/g,'');
    }).join('\\n');
  });
}
setDays(30);
</script></body></html>"""

TEST = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>算法测试</title><style>__CSS__</style></head><body>
__NAV__
<main>
  <h3>已录制的音频（可用最新算法重跑）</h3>
  <table><thead><tr>
    <th>录制目录</th><th>开始</th><th>切片</th><th>大小</th><th>上次结果</th><th></th>
  </tr></thead><tbody id="recs"></tbody></table>

  <h3>转录进度 <span class="muted" id="st"></span></h3>
  <pre id="log">选择一个录制目录并点「用最新算法转录」</pre>

  <h3>转录结果 <span class="muted" id="rcnt"></span></h3>
  <pre id="result"></pre>

  <h3>同时段的历史弹幕 <span class="muted" id="dcnt"></span></h3>
  <pre id="danmaku"></pre>
</main>
<script>
let cur = null, timer = null;
function loadRecs(){
  fetch('/api/recordings').then(r=>r.json()).then(list=>{
    const tb = document.getElementById('recs');
    tb.innerHTML = '';
    list.forEach(x=>{
      const tr = document.createElement('tr');
      tr.innerHTML = '<td>'+x.dir+'</td><td>'+x.started+'</td><td>'+x.files+
        '</td><td>'+x.mb+' MB</td><td>'+(x.has_result?'有':'无')+'</td>'+
        '<td><button class="primary" onclick="run(\\''+x.dir+'\\')">用最新算法转录</button> '+
        '<button onclick="show(\\''+x.dir+'\\')">查看结果</button></td>';
      tb.appendChild(tr);
    });
    if(!list.length) tb.innerHTML='<tr><td colspan="6" class="muted">没有录制目录</td></tr>';
  });
}
function run(dir){
  if(!confirm('对 '+dir+' 跑一遍最新算法？\\n会占用 GPU，一小时音频约需几分钟。')) return;
  cur = dir;
  fetch('/api/transcribe',{method:'POST',headers:{'Content-Type':'application/json'},
       body:JSON.stringify({dir})}).then(r=>r.json()).then(()=>{
    if(timer) clearInterval(timer);
    timer = setInterval(poll, 2000); poll();
  });
}
function show(dir){ cur = dir; poll(); }
function poll(){
  if(!cur) return;
  fetch('/api/transcribe/status?dir='+encodeURIComponent(cur))
    .then(r=>r.json()).then(s=>{
      document.getElementById('st').textContent =
        s.running ? ('运行中 '+s.elapsed+'s') :
        (s.exit_code===null ? '' : ('已结束 code='+s.exit_code));
      document.getElementById('log').textContent = (s.log||[]).join('\\n');
      document.getElementById('log').scrollTop = 9e9;
      if(!s.running && timer){ clearInterval(timer); timer=null; loadResult(); }
      if(!s.running) loadResult(s.kept);
    });
}
function loadResult(kept){
  fetch('/api/transcribe/result?dir='+encodeURIComponent(cur))
    .then(r=>r.json()).then(rows=>{
      document.getElementById('rcnt').textContent = '保留 '+rows.length+' 条';
      document.getElementById('result').innerHTML = rows.map(r=>
        '<span class="t">'+r.clock+'</span> '+r.text.replace(/[<>]/g,'')
      ).join('\\n');
    });
  fetch('/api/danmaku?dir='+encodeURIComponent(cur))
    .then(r=>r.json()).then(rows=>{
      document.getElementById('dcnt').textContent = '共 '+rows.length+' 条';
      document.getElementById('danmaku').innerHTML = rows.map(r=>
        '<span class="t">'+r.clock+'</span> <span class="u">'+(r.uname||'')+
        '</span>: '+(r.text||'').replace(/[<>]/g,'')
      ).join('\\n') || '<span class="muted">该时段没有抓到弹幕</span>';
    });
}
loadRecs();
</script></body></html>"""


KB = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>知识库</title><style>__CSS__</style></head><body>
__NAV__
<main>
  <div class="row">
    <label>房间 <input type="text" id="room" value="__ROOM__" style="width:110px"></label>
    <button class="primary" onclick="loadAll()">刷新</button>
    <button onclick="tab('mem')">记忆</button>
    <button onclick="tab('kb')">事实与人物</button>
    <button onclick="tab('tools')">工具审计</button>
    <button onclick="tab('dec')">决策</button>
  </div>

  <section id="s-mem">
    <h3>机器人记住了什么 <span class="muted" id="mcnt"></span></h3>
    <div class="row">
      <label>筛选
        <select id="pfx" onchange="loadMem()">
          <option value="">全部</option>
          <option value="alias">别名 (alias)</option>
          <option value="note:">观众备注 (note:)</option>
          <option value="pref:">观众偏好 (pref:)</option>
          <option value="fact:">房间事实 (fact:)</option>
          <option value="slang:">房间黑话 (slang:)</option>
          <option value="topic:">本场话题 (topic:)</option>
          <option value="ignore">忽略名单 (ignore)</option>
        </select>
      </label>
      <span class="muted">「依据」列是写入时留的原文片段，用来核对有没有记错</span>
    </div>
    <table><thead><tr>
      <th>key</th><th>值</th><th>依据 / 来源</th><th>优先级</th>
      <th>归属</th><th>写入时间</th>
    </tr></thead><tbody id="mrows"></tbody></table>
  </section>

  <section id="s-kb" style="display:none">
    <h3>事实卡 <span class="muted" id="fmeta"></span></h3>
    <pre id="facts"></pre>
    <h3>人物卡 <span class="muted" id="pcnt"></span></h3>
    <table><thead><tr>
      <th>uid</th><th>昵称</th><th>发言</th><th>印象</th><th>常聊</th>
      <th>风格</th><th>代表发言</th><th>归纳时间</th>
    </tr></thead><tbody id="prows"></tbody></table>
  </section>

  <section id="s-tools" style="display:none">
    <h3>工具调用审计 <span class="muted" id="tcnt"></span></h3>
    <table><thead><tr>
      <th>#</th><th>时间</th><th>工具</th><th>参数</th><th>状态</th>
      <th>拒绝原因</th><th>决策</th>
    </tr></thead><tbody id="trows"></tbody></table>
  </section>

  <section id="s-dec" style="display:none">
    <h3>决策 <span class="muted" id="dcnt"></span></h3>
    <table><thead><tr>
      <th>#</th><th>时间</th><th>触发</th><th>目标</th><th>候选回复</th>
      <th>动作</th><th>原因</th><th>耗时</th><th>会话</th>
    </tr></thead><tbody id="drows"></tbody></table>
  </section>
</main>
<script>
function esc(s){return (s===null||s===undefined)?'':String(s)
  .replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
function room(){return document.getElementById('room').value.trim();}
function tab(which){
  for(const k of ['mem','kb','tools','dec'])
    document.getElementById('s-'+k).style.display = (k===which?'':'none');
}
async function loadMem(){
  const pfx = document.getElementById('pfx').value;
  const rows = await (await fetch('/api/memories?room='+encodeURIComponent(room())
      +'&prefix='+encodeURIComponent(pfx))).json();
  document.getElementById('mcnt').textContent = rows.length + ' 条';
  document.getElementById('mrows').innerHTML = rows.map(r=>
    '<tr><td>'+esc(r.key)+'</td><td>'+esc(r.value)+'</td><td>'
    +esc(r.source)+'</td><td>'+esc(r.priority)+'</td><td>'
    +(r.uid===null?('房间级'):('uid '+esc(r.uid)))+'</td><td>'
    +esc(r.created_at)+'</td></tr>').join('')
    || '<tr><td colspan="6" class="muted">还没有任何记忆</td></tr>';
}
async function loadKb(){
  const d = await (await fetch('/api/kb?room='+encodeURIComponent(room()))).json();
  document.getElementById('facts').textContent = d.facts
    ? JSON.stringify(d.facts, null, 2) : '（还没有事实卡）';
  const m = d.facts_meta || {};
  document.getElementById('fmeta').textContent =
    (m.updated_at?('更新于 '+m.updated_at+'　'):'') + (m.evidence||'');
  document.getElementById('pcnt').textContent = (d.people||[]).length + ' 人';
  document.getElementById('prows').innerHTML = (d.people||[]).map(p=>
    '<tr><td>'+esc(p.uid)+'</td><td>'+esc(p.uname)+'</td><td>'+esc(p.msg_count)
    +'</td><td>'+esc(p.summary)+'</td><td>'+esc(p.topics)+'</td><td>'
    +esc(p.style)+'</td><td>'+esc(p.quote)+'</td><td>'+esc(p.updated_at)
    +'</td></tr>').join('')
    || '<tr><td colspan="8" class="muted">还没有人物卡</td></tr>';
}
async function loadTools(){
  const rows = await (await fetch('/api/tool_calls?limit=100')).json();
  document.getElementById('tcnt').textContent = rows.length + ' 条';
  document.getElementById('trows').innerHTML = rows.map(t=>
    '<tr><td>'+esc(t.id)+'</td><td>'+esc(t.created_at)+'</td><td>'+esc(t.tool)
    +'</td><td>'+esc(t.args)+'</td><td>'+esc(t.status)+'</td><td>'
    +esc(t.reason)+'</td><td>'+esc(t.decision_id)+'</td></tr>').join('')
    || '<tr><td colspan="7" class="muted">还没有工具调用</td></tr>';
}
async function loadDec(){
  const rows = await (await fetch('/api/decisions?limit=60')).json();
  document.getElementById('dcnt').textContent = rows.length + ' 条';
  document.getElementById('drows').innerHTML = rows.map(d=>
    '<tr><td>'+esc(d.id)+'</td><td>'+esc(d.clock)+'</td><td>'+esc(d.trigger)
    +'</td><td>'+esc(d.target_uname)+'</td><td>'+esc(d.draft)+'</td><td>'
    +esc(d.action)+'</td><td>'+esc(d.reason)+'</td><td>'+esc(d.latency_ms)
    +'ms</td><td>'+esc(d.session_id)+'</td></tr>').join('')
    || '<tr><td colspan="9" class="muted">还没有决策记录</td></tr>';
}
async function loadAll(){ await Promise.all([loadMem(),loadKb(),loadTools(),loadDec()]); }
loadAll();
</script></body></html>"""
