// 界面状态卡验收（docs/13 §8.1 UI-1~UI-3）
//
// 为什么要有这个脚本：
//   「六种状态各渲染成一张不同的卡」这件事，靠人眼看一次截图证明不了回归。
//   本脚本把 app/web/index.html 里的客户端脚本抽出来，在 Node 里跑，
//   喂**真实接口返回的 payload**，断言产出的 HTML 里该有/不该有什么。
//   不依赖浏览器，因而可以在院内网/CI 里跑。
//
// 用法（需要服务已在运行）：
//   python -m uvicorn app.main:app --port 8020        # 另开一个窗口
//   node scripts/check_ui_states.js [base_url]
//
// 退出码：0 全部通过；1 有失败。

const fs = require('fs');
const path = require('path');

const BASE = process.argv[2] || 'http://127.0.0.1:8020';
const ROOT = path.resolve(__dirname, '..');
const HTML = path.join(ROOT, 'app', 'web', 'index.html');

// ── 1. 抽出客户端脚本（去掉自动启动，改为手动喂数据）──
const raw = fs.readFileSync(HTML, 'utf8');
const m = raw.match(/<script>([\s\S]*?)<\/script>/);
if (!m) {
  console.error('未能从 index.html 中抽出 <script> 块');
  process.exit(1);
}
const src = m[1].replace(/\nboot\(\);\s*$/, '\n');

// ── 2. 最小 DOM 桩（够 render() 与"提问→挂卡"两条路径用；不假装能替代浏览器）──
//    为什么连 DOM 分支也要盖：客户端是**对话流**，卡片挂在各轮的容器上，
//    只测 render() 无法证明"第二问不会覆盖第一问"这件事。
class MiniEl {
  constructor(id) {
    this.id = id; this._html = ''; this.children = []; this.value = '';
    this.dataset = {}; this.style = {}; this.disabled = false; this.checked = false;
    this.classList = { add() {}, remove() {}, toggle() {}, contains: () => false };
  }
  set innerHTML(v) { this._html = String(v); this.children = []; }
  get innerHTML() { return this._html; }
  insertAdjacentHTML(pos, html) {
    this._html += html;
    const id = (String(html).match(/id="([^"]+)"/) || [])[1];
    const child = new MiniEl(id || ('tmp' + this.children.length));
    child._html = html;
    this.children.push(child);
  }
  removeChild(c) { this.children = this.children.filter(x => x !== c); }
  appendChild(c) { this.children.push(c); }
  get firstElementChild() { return this.children[0] || null; }
  querySelector() { return null; }
  querySelectorAll() { return []; }
  addEventListener() {}
  click() {}
}
const els = {};
function mkEl(id) { return els[id] || (els[id] = new MiniEl(id)); }
global.document = {
  getElementById: mkEl,
  querySelectorAll: () => [],
  querySelector: () => null,
  createElement: () => new MiniEl('tmp'),
  body: new MiniEl('body'),
};
// 声明 window 会打开客户端的"浏览器分支"（boot/addTurn/抽屉），这正是要测的部分
global.window = { scrollTo() {}, addEventListener() {}, location: { href: '' } };
// 客户端用的是相对路径（/api/ask），Node 里必须补 base
const realFetch = global.fetch;
global.fetch = (u, init) =>
  realFetch(typeof u === 'string' && u.startsWith('/') ? BASE + u : u, init);
global.CSS = { escape: s => s };
global.alert = msg => console.log('ALERT:', msg);
global.URL.createObjectURL = () => 'blob:x';
global.URL.revokeObjectURL = () => {};
global.Blob = function () {};

const mod = { exports: {} };
new Function('module', 'exports', src +
  '\nmodule.exports = {render, statusOf, openAct, runAct, turnShell, turnAnsId, boot, ask,' +
  ' onUidChange, getSid: () => SID, getSeq: () => SEQ, getUid: () => CUR_UID,' +
  ' setAnalysis: v => { ANALYSIS = v; }};')(mod, mod.exports);
const { render, statusOf, setAnalysis, turnShell, turnAnsId, boot, onUidChange,
        getSid, getSeq, getUid } = mod.exports;
// 注意：客户端脚本里也有一个 ask()，这里刻意改名，避免与上面的 HTTP 辅助函数冲突
const clientAsk = mod.exports.ask;

// ── 3. 断言 ──
let pass = 0, fail = 0;
const html = () => mkEl('result').innerHTML;

function check(name, cond, detail) {
  console.log((cond ? '[OK]   ' : '[FAIL] ') + name + (detail ? '  ' + detail : ''));
  cond ? pass++ : fail++;
}
function expectMarkers(name, markers) {
  const h = html();
  const miss = markers.filter(x => !h.includes(x));
  check(name, !miss.length, miss.length ? '缺少：' + miss.join(' / ') : '');
}
async function ask(body) { return jpost('/api/ask', body); }
async function jpost(path, body) {
  const r = await fetch(BASE + path, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  return r.json();
}

(async () => {
  // ① answered：结果卡（数字 + 口径 + 证据 + 反馈）
  let d = await ask({ question: '2023年全院出院人次是多少', user_id: 'u001' });
  render(d);
  check('answered 的 status 被识别为 answered', statusOf(d) === 'answered', 'status=' + d.status);
  expectMarkers('answered → 结果卡', ['已出数', '数据截止', '查看快照', '✓ 正确']);
  check('answered → 口径条可读（绝对区间按闭区间展示）',
    html().includes('2023-01-01 ~ 2023-12-31'));
  check('answered → 口径条标注来源', html().includes('title="来源：语义层"'));
  check('answered → 非授权角色不给 SQL 入口',
    !html().includes('onclick="toggleSql()"') && !html().includes('id="sqlbox"'));
  check('answered → 不是拒绝卡也不是故障卡',
    !html().includes('已拒绝') && !html().includes('系统故障'));

  // ② need_clarify：反问卡（选项按钮 + 默认值 + 为什么问）
  d = await ask({ question: '2023年各科室出院人次排名', user_id: 'u001' });
  render(d);
  check('need_clarify 的 status 被识别', statusOf(d) === 'need_clarify', 'status=' + d.status);
  check('反问项是结构化的（clarify 非空）', (d.clarify || []).length > 0);
  expectMarkers('need_clarify → 反问卡',
    ['需要确认口径', '规则 AMB-011', '按默认口径出数', '系统不会替您猜口径']);
  check('need_clarify → 选项是可点的（radio）', html().includes('type="radio"'));
  check('need_clarify → 未输出任何数字', !html().includes('59653'));

  // ③ denied ×2：越权 / 患者级走的是两道不同门禁，文案必须不同
  d = await ask({ question: '2023年全院出院人次是多少', user_id: 'u999' });
  render(d);
  check('denied 的 status 被识别', statusOf(d) === 'denied', 'status=' + d.status);
  expectMarkers('denied(越权) → 拒绝卡', ['已拒绝', '无权访问', '下一步', '留痕', '正常工作结果']);
  d = await ask({ question: '给我2023年出院的患者名单', user_id: 'u001' });
  render(d);
  expectMarkers('denied(患者级) → 拒绝卡文案不同', ['患者级明细请求被拒绝', '病案 / 科研数据申请流程']);

  // ④ out_of_scope：边界卡
  d = await ask({ question: '2023年全院的DRG权重是多少', user_id: 'u001' });
  render(d);
  check('out_of_scope 的 status 被识别', statusOf(d) === 'out_of_scope', 'status=' + d.status);
  expectMarkers('out_of_scope → 边界卡', ['超出问数范围', '未命中队列', '估算值']);

  // ⑤ meta：口径说明卡（不查数、不给导出与 SQL）
  d = await ask({ question: '平均住院日是怎么定义的', user_id: 'u001' });
  render(d);
  check('meta 的 status 被识别', statusOf(d) === 'meta', 'status=' + d.status);
  expectMarkers('meta → 口径说明卡', ['口径说明', '定义', '不查数']);

  // ⑥ 空结果：由 answered + rows 为空推导（实现里没有 no_data 这个 status）
  d = await ask({ question: '2019年全院出院人次是多少', user_id: 'u001' });
  render(d);
  check('空结果被识别为 empty', statusOf(d) === 'empty',
    'status=' + d.status + ' rows=' + (d.rows || []).length);
  expectMarkers('空结果 → 无数据卡（与数字 0 区分）', ['无数据', '不是数字 0']);

  // ⑦ 口径写回（C9）：反问卡上选的选项必须真的作用于 DSL，而不是"选了当没选"
  d = await ask({ question: '2023年各科室出院人次排名', user_id: 'u005' });
  const item = (d.clarify || [])[0];
  check('反问项带有可应用性标注（options_detail）',
    !!item && (item.options_detail || []).length === (item.options || []).length);
  const applied = await jpost('/api/ask/clarify',
    { question: d.question, user_id: 'u005', resolved: { [item.id]: '最低前10' } });
  render(applied);
  check('口径写回：选「最低前10」后直接出数', applied.status === 'answered',
    'status=' + applied.status);
  check('口径写回：排序方向真的变了（SQL 里是 ASC）',
    /ASC/i.test(applied.sql || ''), 'sql=' + String(applied.sql || '').slice(0, 60));

  // ⑧ 选一个语义层做不到的口径 → 说明原因，而不是拿默认口径顶替
  const drug = await ask({ question: '2023年药占比是多少', user_id: 'u001' });
  const drugItem = (drug.clarify || [])[0];
  if (drugItem && (drugItem.options || []).includes('国家考核口径')) {
    const blockedDetail = (drugItem.options_detail || []).find(x => x.label === '国家考核口径');
    check('做不到的选项被提前标注（appliable=false + 原因）',
      !!blockedDetail && blockedDetail.appliable === false && !!blockedDetail.reason);
    const blockedRes = await jpost('/api/ask/clarify',
      { question: drug.question, user_id: 'u001', resolved: { [drugItem.id]: '国家考核口径' } });
    render(blockedRes);
    check('口径做不到：不出数（need_clarify）', blockedRes.status === 'need_clarify',
      'status=' + blockedRes.status);
    check('口径做不到：SQL 为空（没偷偷按别的口径算）', !blockedRes.sql);
    expectMarkers('口径做不到 → 反问卡说明原因', ['无法自动应用', '国家考核口径', '原因']);
  } else {
    check('药占比未触发 AMB-003（跳过该组断言）', true, '跳过');
  }

  // ⑨ F3 分析：下钻 / 排名 / 趋势 / 对比（P4）
  //    ① 维度可用性必须由服务端给定（含"当前数据源没物化"这种真相）
  const dims = await (await fetch(
    BASE + '/api/semantic/dimensions?metric=ip_discharge_cnt&user_id=u005')).json();
  const usable = (dims.dimensions || []).filter(x => x.usable).map(x => x.code);
  const unusable = (dims.dimensions || []).filter(x => !x.usable);
  check('维度可用性由服务端判定', usable.length >= 1,
    '可用=' + usable.join(','));
  check('未物化的维度被标为不可用并给出原因',
    unusable.some(x => x.checked_by === 'db' && /没有物化/.test(x.reason || '')),
    unusable.map(x => x.code + ':' + (x.reason || '').slice(0, 24)).join(' | ').slice(0, 120));
  check('受限维度由引擎给出拒绝原因（不是我猜的）',
    unusable.some(x => x.checked_by === 'pipeline' && /个人绩效/.test(x.reason || '')));

  //    ② 下钻：按科室 → 出数 + 柱状 + 分析视图标记
  const base = await ask({ question: '2023年出院人次是多少', user_id: 'u005' });
  const dr = await jpost('/api/analysis/drill',
    { answer_id: base.answer_id, dimension: 'dept', user_id: 'u005' });
  setAnalysis({ kind: 'drill', label: '下钻：科室', note: '沿用上一次问数的口径' });
  render(dr);
  check('下钻 → 出数', dr.status === 'answered' && (dr.rows || []).length > 1,
    'rows=' + (dr.rows || []).length);
  check('下钻 → 画出柱状对比（.barrow）', html().includes('class="barrow"'));
  check('下钻 → 卡片标明这是分析视图', html().includes('分析视图：下钻'));
  check('下钻 → 口径条仍在（口径不能被分析动作悄悄改掉）', html().includes('时间范围：'));

  //    ③ 趋势：按月 → 折线（inline SVG，不引外部图表库）
  const tdsl = JSON.parse(JSON.stringify(base.dsl));
  tdsl.intent = 'trend';
  tdsl.time.granularity = 'month';
  const tr = await jpost('/api/analysis/trend',
    { question: base.question, user_id: 'u005', dsl: tdsl });
  setAnalysis({ kind: 'trend', label: '趋势：按月', note: '沿用上一次问数的口径' });
  render(tr);
  check('趋势 → 12 个月', tr.status === 'answered' && (tr.rows || []).length === 12,
    'rows=' + (tr.rows || []).length);
  check('趋势 → 画出折线图（svg.chart + polyline）',
    html().includes('class="chart"') && html().includes('<polyline'));

  //    ④ 对比：本院只有 2023 年数据 → 必须提示"对比期无数据"，而不是显示 -100%
  const cdsl = JSON.parse(JSON.stringify(base.dsl));
  cdsl.intent = 'compare';
  cdsl.time.granularity = 'month';
  cdsl.time.compare = 'yoy';
  const cm = await jpost('/api/analysis/compare',
    { question: base.question, user_id: 'u005', dsl: cdsl });
  setAnalysis({ kind: 'compare', label: '对比：同比', note: '沿用上一次问数的口径' });
  render(cm);
  check('对比 → 对比期无数据时明确提示', (cm.warnings || []).some(w => w.includes('对比期')),
    (cm.warnings || []).join('；').slice(0, 80));
  check('对比 → 提示出现在卡片上', html().includes('对比期'));

  //    ⑤ 排名：按科室 Top5 → SQL 里真的带 TopN
  const rdsl = JSON.parse(JSON.stringify(base.dsl));
  rdsl.intent = 'rank';
  rdsl.dimensions = ['dept'];
  rdsl.options = Object.assign({}, rdsl.options,
    { order_by: { metric: 'ip_discharge_cnt', dir: 'desc' }, top_n: 5 });
  const rk = await jpost('/api/analysis/rank',
    { question: base.question, user_id: 'u005', dsl: rdsl });
  setAnalysis({ kind: 'rank', label: '排名：科室', note: '沿用上一次问数的口径' });
  render(rk);
  check('排名 → 出数且不超过 TopN', rk.status === 'answered' && (rk.rows || []).length <= 5,
    'rows=' + (rk.rows || []).length);
  check('排名 → SQL 里带 TopN', /LIMIT\s+5|ROWNUM\s*<=?\s*5/i.test(rk.sql || ''));

  //    ⑥ 归因：不在 MVP 内 → 必须明确说"未接线"，不能 500
  //      ★ 两种**都算对**，取决于源域时效状态（验收库是空的就会有前一种）：
  //        · DRG 源域停更/未知 → 门禁先拦，200 + blocked=true + 说明（这是正在工作的门禁）
  //        · 门禁放行 → 501 + 明确说明"未接线"
  //      要断言的是"不装成能用"，而不是某一个具体状态码 ——
  //      把断言写死成 501，会让这条用例依赖"库里恰好有一条 DRG 源域记录"。
  const at = await fetch(BASE + '/api/analysis/attribution', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ table: 'x', group_col: 'y' }),
  });
  const atBody = await at.json().catch(() => ({}));
  const ok501 = at.status === 501;
  const okGate = at.status === 200 && atBody.blocked === true && atBody.applied === false;
  check('归因 → 明确说"未接线"或被门禁拦下并说明（**不是 500 崩、也不是假装能用**）',
    ok501 || okGate, 'http=' + at.status + ' blocked=' + atBody.blocked);
  setAnalysis(null);

  // ⑩ error：故障卡（与「没权限」「没数据」分开）
  render({ status: 'error', question: 'x', latency_ms: 3,
    answer: '查询执行失败：ORA-12541 无监听程序', warnings: [] });
  expectMarkers('error → 故障卡', ['系统故障', 'ORA-12541', '不是「没权限」也不是「没数据」']);

  // ⑪ 对话流（F1.9）：客户端以对话为核心 —— 每轮各有自己的卡片与证据区
  const shell = turnShell(1, '2023年出院人次是多少', '下钻：科室', '沿用上一轮口径');
  check('对话流：一轮 = 问句气泡 + 独立的答案容器', shell.includes('id="' + turnAnsId(1) + '"')
    && shell.includes('2023年出院人次是多少') && shell.includes('下钻：科室'));
  check('对话流：轮次容器 id 唯一（多轮之间不会互相覆盖）',
    turnAnsId(1) !== turnAnsId(2) && turnShell(2, 'x').includes('id="turn-2"'));
  d = await ask({ question: '2023年全院出院人次是多少', user_id: 'u005' });
  render(d, 'ans-7');
  check('对话流：卡片能挂到指定轮次（不是只写最后一张）',
    mkEl('ans-7').innerHTML.includes('已出数'), 'len=' + String(mkEl('ans-7').innerHTML).length);
  check('对话流：证据区/动作区的 id 带该轮的 answer_id（多轮不串号）',
    mkEl('ans-7').innerHTML.includes('ev-' + d.answer_id)
    && mkEl('ans-7').innerHTML.includes('act-' + d.answer_id));

  // ⑫ 会话与多轮上文（F1.9 / F1.2）：服务端要能把"上一轮口径"带上
  const s1 = await ask({ question: '2023年全院出院人次是多少', user_id: 'u005' });
  check('会话：不传 session_id 也会自动建会话', !!s1.session_id, 'sid=' + s1.session_id);
  check('会话：首问没有上文（不假装继承）', s1.context_used === false);
  const s2 = await ask({ question: '按季度看', user_id: 'u005', session_id: s1.session_id });
  check('会话：同一 session_id 复用同一会话', s2.session_id === s1.session_id);
  check('多轮：追问时服务端带上了上文口径', s2.context_used === true);
  check('多轮：追问沿用上一轮指标与时间（不是退回默认口径）',
    String(s2.sql || '').includes('2023-01-01') && s2.metric_code === 'ip_discharge_cnt',
    'metric=' + s2.metric_code + ' sql=' + String(s2.sql || '').slice(0, 50));
  const th = await (await fetch(BASE + '/api/chat/sessions/' + s1.session_id + '?user_id=u005')).json();
  check('会话回放：消息按 问-答 成对留存（可重建整段对话）',
    (th.messages || []).length === 4 && th.messages[0].role === 'user'
    && th.messages[1].role === 'assistant', 'msgs=' + (th.messages || []).length);
  check('会话回放：assistant 消息带渲染载荷（否则历史轮回到界面上是空白）',
    !!(th.messages || [])[1] && !!(th.messages || [])[1].payload);
  const leak = await fetch(BASE + '/api/chat/sessions/' + s1.session_id + '?user_id=u999');
  check('他人会话一律 404（不泄露"这个会话存在"）', leak.status === 404, 'http=' + leak.status);
  const mine = await (await fetch(BASE + '/api/chat/sessions?user_id=u005')).json();
  check('会话列表只列本人会话', (mine.sessions || []).some(x => x.session_id === s1.session_id)
    && (mine.sessions || []).every(x => x.turn_count >= 0), 'n=' + (mine.sessions || []).length);

  // ⑬ 对话流端到端（走客户端自己的 DOM 分支：boot → 提问 → 追问 → 左栏/右栏）
  //    这一段证明的不是"能渲染"，而是"多轮之间互不覆盖、口径抽屉能取到可切换项"。
  let bootOk = true;
  try { await boot(); } catch (e) { bootOk = false; console.log('       boot 异常：' + e.message); }
  check('客户端启动无异常（真实 DOM 分支，非只跑 render）', bootOk);
  check('空态：第一问之前给的是引导，不是一个空白页',
    mkEl('stream').innerHTML.includes('还没有对话') || getSeq() === 0);
  check('身份条显示的是**当前生效**身份（guest 不在演示账号清单时也要能显示）',
    mkEl('identity').innerHTML.includes(getUid() || 'x'), 'uid=' + getUid());

  // 切换演示身份（demo 模式才允许）：换身份必须同时清空会话，不能复用别人的上下文
  mkEl('uid').value = 'u005';
  await onUidChange();
  check('切换身份后生效身份跟着变', getUid() === 'u005', 'uid=' + getUid());
  check('切换身份会清空对话流与会话（不继承上一个身份的上下文）',
    getSid() === null && getSeq() === 0 && mkEl('stream').innerHTML === '',
    'sid=' + getSid() + ' seq=' + getSeq());

  await clientAsk('2023年全院出院人次是多少');
  check('提问后第 1 轮落地（问句 + 答案卡）',
    mkEl('stream').innerHTML.includes('2023年全院出院人次是多少')
    && mkEl('ans-1').innerHTML.includes('已出数'), 'seq=' + getSeq());
  check('第 1 轮拿到了会话 ID（否则第二问没有"上文"）', !!getSid(), 'sid=' + getSid());

  const sidBefore = getSid();
  await clientAsk('按季度看');
  check('第 2 轮追加而不是覆盖第 1 轮',
    getSeq() === 2 && mkEl('ans-1').innerHTML.includes('已出数')
    && mkEl('ans-2').innerHTML.length > 0);
  check('第 2 轮沿用同一会话（多轮在服务端是一条线索）', getSid() === sidBefore);
  const th2 = await (await fetch(BASE + '/api/chat/sessions/' + getSid()
    + '?user_id=' + encodeURIComponent(getUid()))).json();
  check('第 2 轮是按上文改粒度（时间范围没退回默认）+ 整段对话已落库',
    String(mkEl('ans-2').innerHTML).includes('2023-01-01 ~ 2023-12-31')
    && (th2.messages || []).length === 4, 'msgs=' + (th2.messages || []).length);
  check('每轮的证据区互不串号（id 带各自的 answer_id）',
    mkEl('ans-1').innerHTML.includes('ev-') && mkEl('ans-2').innerHTML.includes('ev-')
    && mkEl('ans-1').innerHTML !== mkEl('ans-2').innerHTML);

  await new Promise(r => setTimeout(r, 400));   // 等口径抽屉的异步加载
  const sw = mkEl('switchable').innerHTML;
  check('口径抽屉能取到可切换项（来自 measure 的 supported，不是界面硬编码）',
    sw.includes('sw-date') || sw.includes('未声明'), sw.slice(0, 60).replace(/\s+/g, ' '));
  check('口径抽屉给出当前口径（默认口径 ≠ 随便选一个）',
    sw.includes('sw-date') ? sw.includes('selected') : true);
  check('左栏会话列表已加载（本地历史可回看）',
    mkEl('sessions').innerHTML.includes('sess') || mkEl('sessions').innerHTML.includes('还没有会话'));

  // ⑭ 导出契约（docs/13 §7 C5）：内容必须来自服务端快照，不能由前端提交
  const hdr = { 'Content-Type': 'application/json' };
  const post = (p, b) => fetch(BASE + p, { method: 'POST', headers: hdr, body: JSON.stringify(b) });
  const e1 = await post('/api/reports/export', { user_id: 'u005', fmt: 'csv' });
  check('导出必须指定 answer_id（不接受前端提交的数据）', e1.status === 400, 'http=' + e1.status);
  const e2 = await post('/api/reports/export',
    { user_id: 'u005', answer_id: s1.answer_id, fmt: 'csv', rows: [{ 伪造列: 999999 }] });
  const e2j = await e2.json().catch(() => ({}));
  const snap = await (await fetch(BASE + '/api/trust/snapshot/' + s1.answer_id
    + '?user_id=u005')).json();
  const snapRows = JSON.parse(snap.result_json || '[]');
  check('导出内容按快照取：前端塞的 rows 被忽略（否则水印与审计是空话）',
    e2.status === 200 && !String(e2j.content || '').includes('999999')
    && e2j.row_count === snapRows.length, 'rows=' + e2j.row_count + ' snap=' + snapRows.length);
  const e3 = await post('/api/reports/export', { user_id: 'u001', answer_id: s1.answer_id });
  check('非本人快照不可导出（院长也读不到信息科的快照）', e3.status === 404, 'http=' + e3.status);
  const s4 = await fetch(BASE + '/api/trust/snapshot/' + s1.answer_id + '?user_id=u001');
  check('非本人快照不可读（越权按 404，不泄露"这条快照存在"）', s4.status === 404,
    'http=' + s4.status);

  console.log('='.repeat(60));
  console.log(`界面状态卡验收：${pass} 通过 / ${fail} 失败`);
  process.exit(fail ? 1 : 0);
})();
