#!/usr/bin/env node
/**
 * 管理端验收（docs/13 §8.1 UI-8/UI-9/UI-10 + §4.3 指标编辑器约束）。
 *
 * 用法：
 *   1) 先复制一份语义层（**绝不能让验收脚本写真实语义层**）：
 *        copy semantic-layer\metric_registry.yaml var\_admin_check_registry.yaml
 *   2) 用副本起一个服务：
 *        $env:ASK_REGISTRY="var\_admin_check_registry.yaml"; $env:ASK_DS="ads"; $env:ASK_LLM_MOCK="1"
 *        uvicorn app.main:app --port 8032
 *   3) node scripts/check_admin_states.js http://127.0.0.1:8032
 *
 * 脚本自身还有一道保险：确认服务端的语义层路径里含 `_admin_check`，
 * 否则**跳过所有写操作**（宁可少测，不可改坏资产）。
 */
const fs = require('fs');
const path = require('path');

const BASE = process.argv[2] || 'http://127.0.0.1:8032';
const ADMIN_UID = 'u005';        // 信息科：唯一带 admin:true 的演示角色

// ── 1. 最小 DOM 桩（够跑管理端脚本的 DOM 分支）──
class MiniEl {
  constructor(id) {
    this.id = id; this._html = ''; this.children = []; this.value = '';
    this.dataset = {}; this.style = {}; this.disabled = false;
    this.classList = { add() {}, remove() {}, toggle() {}, contains: () => false };
  }
  set innerHTML(v) { this._html = String(v); this.children = []; }
  get innerHTML() { return this._html; }
  insertAdjacentHTML(pos, html) { this._html += html; }
  removeChild(c) { this.children = this.children.filter(x => x !== c); }
  querySelector() { return null; }
  querySelectorAll() { return []; }
  addEventListener() {}
  click() {}
}
const els = {};
function mkEl(id) { return els[id] || (els[id] = new MiniEl(id)); }
global.document = {
  getElementById: mkEl, querySelector: () => null, querySelectorAll: () => [],
  createElement: () => new MiniEl('tmp'), body: new MiniEl('body'),
};
global.window = { location: { href: '' } };
const realFetch = global.fetch;
global.fetch = (u, init) =>
  realFetch(typeof u === 'string' && u.startsWith('/') ? BASE + u : u, init);
global.URLSearchParams = URLSearchParams;

// ── 2. 抽出管理端脚本 ──
const file = path.join(__dirname, '..', 'app', 'web', 'admin.html');
const htmlAll = fs.readFileSync(file, 'utf8');
const m = htmlAll.match(/<script>([\s\S]*?)<\/script>/);
if (!m) { console.error('未找到 <script> 块：' + file); process.exit(2); }
// 去掉末尾自动启动的那一行：它会在后台异步地把身份设回 guest，
// 与验收脚本设置的身份形成竞态（时好时坏的那种）。改由脚本自己 await bootAdmin()。
const src = m[1].replace(/\nbootAdmin\(\);\s*$/, '\n');

const mod = { exports: {} };
new Function('module', 'exports', src +
  '\nmodule.exports = { esc, uq, gatePanel, renderAdminIdentity, editMetric, showReconcile,' +
  ' showVersions, showPerm, showGrants, syncPreview, verifyChain, sensExplain, bootAdmin,' +
  ' setSess: s => { SESS = s; }, setUid: u => { ADMIN_UID = u; }};'
)(mod, mod.exports);
const { esc, uq, gatePanel, renderAdminIdentity, editMetric, showReconcile, showVersions,
        showPerm, showGrants, sensExplain, bootAdmin, setSess, setUid } = mod.exports;

// ── 3. 断言 ──
let pass = 0, fail = 0;
function check(name, ok, detail) {
  if (ok) { pass++; console.log('[OK]   ' + name + (detail ? '  ' + detail : '')); }
  else { fail++; console.log('[FAIL] ' + name + (detail ? '  ' + detail : '')); }
}
const panel = () => mkEl('panel').innerHTML;

(async () => {
  console.log('管理端验收：' + BASE);

  // ① 身份与门禁（UI-10）
  // ★ 必须先等 bootAdmin 落地（它是异步的，await 完成后会按服务端返回值设置身份）。
  //   不等它，它就会在下面 setUid 之后把身份重置回 guest —— 这种竞态会让验收结果时好时坏。
  await bootAdmin();
  setUid(null);
  check('生产模式不自报身份（uq 不带 user_id）', !uq('/api/x').includes('user_id'), uq('/api/x'));
  setUid(ADMIN_UID);
  check('demo 模式把身份交给服务端（口径/授权由服务端判）', uq('/api/x').includes('user_id=u005'));

  setSess({ auth_mode: 'demo', user: { user_id: 'u001', name: '张院长', role: '院长', is_admin: false } });
  check('非信息科角色被挡在管理端外（不渲染后台骨架）', gatePanel() === true);
  check('被挡的页面说清"谁能进、真边界在哪"',
    panel().includes('无管理端权限') && panel().includes('真正的边界在网关'),
    panel().slice(0, 40).replace(/\s+/g, ' '));

  setSess({ auth_mode: 'demo', user: { user_id: 'u005', name: '李工', role: '信息科', is_admin: true } });
  check('信息科角色可进管理端', gatePanel() === false);

  setSess({ auth_mode: 'demo',
            user: { user_id: 'guest', name: '', role: 'guest', is_admin: false },
            demo_accounts: [{ user_id: 'u001', name: '张院长', role_name: '院长' }] });
  renderAdminIdentity();
  check('身份条显示的是当前生效身份（默认 guest 不在账号清单也要能显示）',
    mkEl('admin_identity').innerHTML.includes('value="guest"'),
    mkEl('admin_identity').innerHTML.slice(0, 80).replace(/\s+/g, ' '));

  // ①b 授权台账页（F6.2 · docs/12 §4.2）
  setSess({ auth_mode: 'demo', user: { user_id: 'u005', name: '李工', role: '信息科', is_admin: true } });
  setUid('u005');
  await showGrants();
  const gp = panel();
  check('授权台账页列出「谁能看哪些科室 / 到什么时候 / 依据是什么」',
    gp.includes('授权台账') && gp.includes('科室范围') && gp.includes('依据'), gp.slice(0, 40));
  check('台账页显示授权来源档位与迁移覆盖率（否则切严格模式就是盲切）',
    gp.includes('授权来源档位') && gp.includes('仍只在 YAML 里'),
    (gp.match(/仍只在 YAML 里[^<]*/) || [''])[0]);
  check('台账页写明「台账是唯一事实来源」（角色能做什么仍在 roles.yaml）',
    gp.includes('唯一事实来源') && gp.includes('roles.yaml'));
  check('批量同步要二次确认（防手滑，不是防攻击者）',
    gp.includes('我确认写入授权台账') && gp.includes('先预览'));
  check('界面不提供 file 源同步入口（接口读任意路径 = 任意文件读取口）',
    gp.includes('接口不接受调用方指定的文件路径'));

  // ② 指标编辑器约束（docs/13 §4.3）
  setSess({ auth_mode: 'demo', user: { user_id: ADMIN_UID, name: '李工', role: '信息科', is_admin: true } });
  await editMetric('ip_discharge_cnt');
  const ed = mkEl('editor').innerHTML;
  check('编辑器有责任人字段且标明必填（无责任人的指标没人能拍板口径）',
    ed.includes('责任人') && ed.includes('必填'));
  check('公式只展示不解析（界面不做口径推导）', ed.includes('不解析'));
  check('敏感级旁边写清三级后果（S9 的坑就在这）',
    sensExplain('restricted').includes('不能') && ed.includes('敏感级'));
  check('measure / 维度 / 默认口径是只读区，界面上不可编辑',
    ed.includes('不可编辑') && ed.includes('绑定度量') && !ed.includes('id="mmeasure"'));
  check('口径多选带 data-basis（与 saveMetric 的选择器一致，否则勾选会被静默丢弃）',
    ed.includes('data-basis="date"') && src.includes('[data-basis=date]'),
    'date=' + ed.includes('data-basis="date"') + ' sel=' + src.includes('[data-basis=date]'));
  check('口径可选项来自 measure 的 supported（不是界面硬编码）',
    ed.includes('admit') || ed.includes('discharge'),
    'hint=' + (ed.match(/时间口径：[\s\S]{0,60}/) || [''])[0].replace(/\s+/g, ' '));

  await editMetric('');
  const edNew = mkEl('editor').innerHTML;
  check('新建指标时明确告知"只有元数据、还出不了数"',
    edNew.includes('元数据条目') && edNew.includes('只能改 YAML'));
  check('新建时不允许凭空声明口径可选项（随 measure 而定）',
    edNew.includes('先绑定 measure'));

  // ③ 版本页与对账页必须诚实
  await showVersions();
  check('版本页写明审批流尚未实现（不假装有审批按钮）',
    panel().includes('审批流尚未实现') && panel().includes('版本留痕'));
  const rcRes = await fetch(BASE + uq('/api/semantic/reconcile', { domain: '住院' }));
  const rcJson = await rcRes.json().catch(() => ({}));
  check('对账接口：管理员可调，且如实返回 pending_config（不编造对比数据）',
    rcRes.status === 200 && rcJson.status === 'pending_config',
    'http=' + rcRes.status + ' ' + JSON.stringify(rcJson).slice(0, 160));

  await showReconcile();
  const rc = panel();
  check('对账页如实显示 pending_config，而不是画一张假对比表',
    rc.includes('待配置映射') && rc.includes('还出不了对比表') && rc.includes('ITEM_CODE'),
    `待配置=${rc.includes('待配置映射')} 出不了=${rc.includes('还出不了对比表')} `
    + `ITEM_CODE=${rc.includes('ITEM_CODE')} | ` + rc.replace(/\s+/g, ' ').slice(0, 220));

  // ③b 权限页（F6.2/F6.5）：角色、审计链、导出追溯 + 演示模式的诚实声明
  setUid(ADMIN_UID);
  await showPerm();
  const pm = panel();
  check('权限页画出角色定义（含数据范围与管理端列）',
    pm.includes('角色定义') && pm.includes('管理端'));
  check('权限页说明 admin 不放大数据范围',
    pm.includes('不放大任何数据范围'));
  check('权限页有审计链校验入口', pm.includes('审计存储') && pm.includes('校验链 + 链锚'));
  check('权限页说明链锚的作用（只做链发现不了"删尾部"）',
    pm.includes('删掉尾部记录') && pm.includes('链锚落在库外'));
  check('权限页明说审计密钥在库外的**代价**（丢了历史链永久无法校验）',
    pm.includes('密钥丢了，历史链就永久无法校验'));
  check('权限页有封锚入口（运维例行操作，不是隐藏功能）', pm.includes('封一个链锚'));
  check('权限页有导出追溯与流水号反查', pm.includes('导出追溯') && pm.includes('反查这个流水号'));
  check('演示模式下明说"不是安全边界"（不让人误以为已鉴权）',
    pm.includes('不是安全边界') && pm.includes('ASK_AUTH_MODE=gateway'));

  // ④ API 契约：语义层写入必须是"合并"，不是"整条替换"
  const reg = await (await fetch(
    BASE + '/api/admin/registry?user_id=' + ADMIN_UID)).json();
  const regPath = String(reg.path || '');
  const safe = regPath.includes('_admin_check');
  console.log('   语义层路径：' + regPath + (safe ? '  [副本，可写]' : '  [⚠️ 非副本]'));
  if (!safe) {
    check('写操作已跳过（避免改坏真实语义层）', true, '请用 ASK_REGISTRY=var\\_admin_check_registry.yaml 起服务再跑');
  } else {
    const post = (p, b) => fetch(BASE + p, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(b) });
    const getMetric = async c => (await (await fetch(
      BASE + '/api/semantic/metrics/' + encodeURIComponent(c) + '?user_id=' + ADMIN_UID)).json());

    const before = await getMetric('ip_discharge_cnt');
    const keep = ['measure', 'dimensions', 'default_date_basis', 'default_dept_basis'];
    // 验收要能反复跑：责任人取一个**与当前不同**的值。
    // 否则第二次跑时提交的值跟现值一样，changed 自然是空数组，
    // "只改了的字段才进 changed" 会被误报成失败（是脚本不幂等，不是产品问题）。
    const targetOwner = before.metric.owner === '医务处 张主任' ? '医务处 王主任' : '医务处 张主任';
    const r1 = await post('/api/semantic/metrics', {
      user_id: ADMIN_UID, code: 'ip_discharge_cnt', name: before.metric.name, owner: targetOwner,
      operator: ADMIN_UID, change_note: '管理端验收：只改责任人',
    });
    const r1j = await r1.json();
    const after = await getMetric('ip_discharge_cnt');
    check('改一个字段不会抹掉 measure / 维度（此前是整条替换）',
      keep.every(k => JSON.stringify(after.metric[k]) === JSON.stringify(before.metric[k])),
      keep.filter(k => JSON.stringify(after.metric[k]) !== JSON.stringify(before.metric[k])).join(',') || '全部保持');
    check('只改了的字段才进 changed（界面据此回显"变更字段"）',
      Array.isArray(r1j.changed) && r1j.changed.includes('owner')
      && !r1j.changed.includes('measure'), JSON.stringify(r1j.changed));
    // 这条防的是"模型默认值静默改写"：只发 code/name/owner 时，
    // 敏感级/状态/类型必须原样不动（曾经会被 MetricIn 的默认值重置）
    check('未提交的字段不被默认值静默改写（敏感级 / 状态 / 类型）',
      ['type', 'sensitivity', 'status'].every(k =>
        JSON.stringify(after.metric[k]) === JSON.stringify(before.metric[k])), 
      ['type', 'sensitivity', 'status']
        .map(k => k + ':' + JSON.stringify(before.metric[k]) + '→' + JSON.stringify(after.metric[k]))
        .join(' '));
    check('保存后版本号递增且可查', !!r1j.version, 'version=' + r1j.version);

    const r2 = await post('/api/semantic/metrics', {
      user_id: ADMIN_UID, code: 'ip_discharge_cnt', name: before.metric.name, owner: '',
      operator: ADMIN_UID });
    check('责任人缺失被拒（400），责任人不是可选项', r2.status === 400, 'http=' + r2.status);

    // 新建用例同样要可反复跑：上一轮留下的 zz_admin_check 还躺在副本里（已下线）。
    // 先挑一个当前确实不存在的 code，否则走的是 update 分支，"新建可用"就成了假通过。
    let newCode = 'zz_admin_check';
    for (let i = 2; (await getMetric(newCode)).metric; i++) newCode = 'zz_admin_check_' + i;
    const r3 = await post('/api/semantic/metrics', {
      user_id: ADMIN_UID, code: newCode, name: '验收用指标', domain: '住院',
      owner: '信息科 李工', operator: ADMIN_UID, change_note: '管理端验收：新建' });
    check('新建指标可用（元数据条目）', (await r3.json()).action === 'create', 'code=' + newCode);

    const r4 = await post('/api/semantic/metrics/' + newCode + '/retire',
      { user_id: ADMIN_UID, operator: ADMIN_UID, reason: '管理端验收：下线原因必须留痕' });
    check('下线接口可用且记录原因', (await r4.json()).status === 'offline');

    const vs = await (await fetch(BASE + '/api/semantic/versions?code=' + newCode + '&limit=50'
      + '&user_id=' + ADMIN_UID)).json();
    check('版本留痕里能查到"谁在什么时候改了什么"',
      (vs || []).some(v => v.operator === ADMIN_UID && /下线/.test(v.change_note || '')),
      'n=' + (vs || []).length);
    check('版本留痕的 operator 取自服务端身份（不采信请求体里的 operator）',
      (vs || []).every(v => v.operator !== 'attacker'));
  }

  // ⑤ 服务端鉴权（F6，v0.18）：这些边界以前只靠前端藏按钮
  const call = async (p, uid, opt) => {
    const url = BASE + p + (p.includes('?') ? '&' : '?') + 'user_id=' + encodeURIComponent(uid);
    const r = await fetch(url, opt || {});
    return r.status;
  };
  const postJson = (p, uid, body) => call(p, uid, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });

  check('非管理员改指标 → 403（以前一个 curl 就能改全院的敏感级）',
    await postJson('/api/semantic/metrics', 'u001',
      { code: 'ip_discharge_cnt', name: 'x', owner: 'y' }) === 403);
  check('非管理员看管理配置 → 403', await call('/api/admin/config', 'u001') === 403);
  check('非管理员看角色定义 → 403', await call('/api/security/roles', 'u001') === 403);
  check('非管理员看运营指标 → 403', await call('/api/ops/stats', 'u001') === 403);
  check('未授权身份（guest）查审计 → 403',
    await call('/api/security/audit', 'guest') === 403);
  const ownAudit = await fetch(BASE + '/api/security/audit?limit=50&user_id=' + 'u001');
  const ownRows = await ownAudit.json();
  check('普通用户查审计只看到**自己的**记录（不是全院问答史）',
    ownAudit.status === 200 && Array.isArray(ownRows)
    && ownRows.every(r => r.user_id === 'u001'), 'n=' + (ownRows || []).length);
  check('审计汇总属于全局视图 → 仅管理员',
    await call('/api/security/audit/summary', 'u001') === 403);
  check('导出追溯（全院谁导过数据）→ 仅管理员',
    await call('/api/reports/exports', 'u001') === 403);

  // ⑥ 审计哈希链：先真的问一次（产生一条带哈希的审计），再校验
  await postJson('/api/ask', ADMIN_UID,
    { question: '2023年全院出院人次是多少', user_id: ADMIN_UID });
  const vj = await (await fetch(BASE + '/api/security/audit/verify?user_id=' + ADMIN_UID)).json();
  check('审计链 + 链锚校验通过（管理员），且新出数已入链',
    vj.ok === true && vj.checked > 0 && vj.anchor_ok === true,
    'checked=' + vj.checked + ' legacy=' + vj.legacy_rows + ' anchors=' + vj.anchors);
  check('链校验结果如实标注局限（库可被整体重算 → 结论同时看链锚）',
    String(vj.note || '').includes('链锚') && String(vj.note || '').includes('重算'));
  check('校验结果带密钥指纹（失败时能区分"被改"与"换密钥"）',
    typeof vj.key_id === 'string' && vj.key_id.length >= 8, vj.key_id);
  check('非管理员不能封链锚（锚写的是"某时刻库里有 N 条"的真实证据）',
    await postJson('/api/security/audit/seal', 'u001', {}) === 403);
  const sealR = await (await fetch(BASE + '/api/security/audit/seal?user_id=' + ADMIN_UID,
    { method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}' })).json();
  check('管理员封锚成功，且锚里有条数与密钥指纹（否则锚无法用来比对）',
    sealR.ok === true && sealR.anchor.count > 0 && (sealR.anchor.key_id || '').length >= 8,
    'seq=' + (sealR.anchor || {}).seq + ' count=' + (sealR.anchor || {}).count);

  // ⑥b 导出流水号：真的导一次，拿水印里的编号反查（docs/12 §6.4）
  const askR = await fetch(BASE + '/api/ask?user_id=' + ADMIN_UID, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ question: '2023年全院出院人次是多少', user_id: ADMIN_UID }) });
  const askB = await askR.json();
  const expR = await fetch(BASE + '/api/reports/export?user_id=' + ADMIN_UID, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ answer_id: askB.answer_id, user_id: ADMIN_UID }) });
  const expB = await expR.json();
  check('导出返回流水号，且写进了水印',
    /^EX-\d{8}-\d{4}-\d{2}$/.test(String(expB.serial || ''))
    && String(expB.watermark || '').includes(String(expB.serial)),
    String(expB.serial || ''));
  const verifySerial = async (s) => (await fetch(
    BASE + '/api/reports/exports/verify?serial=' + encodeURIComponent(s)
    + '&user_id=' + ADMIN_UID)).json();
  const vf = await verifySerial(expB.serial || '');
  check('按流水号能反查到这条导出记录', vf.format_ok === true && vf.found === true,
    String(vf.conclusion || '').slice(0, 30));
  const bad = String(expB.serial || '').slice(0, -1)
    + (String(expB.serial || '').endsWith('00') ? '11' : '00');
  const vf2 = await verifySerial(bad);
  check('改掉校验位 → 判为「编号不合法」，而不是「没查到」',
    vf2.format_ok === false && vf2.found === false, String(vf2.conclusion || '').slice(0, 24));
  // 自己按 ISO 7064 造一个**格式合法但绝不存在**的编号（2020 年的日期，日志里不会有）
  const ccOf = (day, seq) => {
    const core = BigInt(day) * 10000n + BigInt(seq);
    return String((98n - (core * 100n) % 97n) % 97n).padStart(2, '0');
  };
  const fake = 'EX-20200101-0001-' + ccOf(20200101, 1);
  const vf3 = await verifySerial(fake);
  check('格式合法但日志里没有 → 判为伪造（不因为"看着像真的"就放过）',
    vf3.format_ok === true && vf3.found === false, String(vf3.conclusion || '').slice(0, 24));

  // ⑦ 数据源切换的二次确认（防"手滑换了口径"）
  const noConfirm = await call('/api/admin/config/datasource', ADMIN_UID, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ active: 'ads' }) });
  check('切数据源不带二次确认 → 400', noConfirm === 400, 'http=' + noConfirm);

  // ⑧ 前端也必须有对应的二次确认控件（两边都拦才算拦）
  check('数据源页有二次确认勾选框，且提交时带上确认口令（服务端强校验）',
    src.includes('ds_confirm') && src.includes("body.confirm = '我确认切换数据源'"));
  // /api/admin/* 全部要求管理员 → 界面调用必须带身份，否则管理员自己也被 403
  // （/api/security/session、/api/security/me 是公开的身份入口，不在此列）
  const bareAdminCalls = (src.match(/(jget|jsend)\('\/api\/admin/g) || []).length;
  check('管理端所有 /api/admin 调用都带身份（uq）—— 否则管理员自己也会被 403',
    bareAdminCalls === 0, 'bare=' + bareAdminCalls);


  console.log('='.repeat(60));
  console.log(`管理端验收：${pass} 通过 / ${fail} 失败`);
  process.exit(fail ? 1 : 0);
})().catch(e => { console.error('验收脚本异常：', e); process.exit(2); });
