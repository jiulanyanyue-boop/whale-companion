const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;
const $ = (id) => document.getElementById(id);
const controls = ['setup', 'save', 'install', 'uninstall', 'start', 'join', 'stop', 'enabled', 'game-dir', 'port',
  'transport', 'share-address', 'address-candidate', 'lan-install', 'lan-uninstall', 'copy-invite',
  'connection', 'public-port', 'relay-host', 'relay-control-port', 'relay-prepare', 'relay-download',
  'relay-install', 'relay-start', 'relay-stop'];
let busy = false;
let refreshing = false;
let dirty = false;
let state = {};
let addressSignature = '';

const errors = {
  configure_valid_game_directory_first: '请填写包含 SlayTheSpire2.exe 的游戏目录并保存。',
  unmanaged_mod_file_exists: '检测到手动安装的同名 Mod，未覆盖。请先备份并移走它，或继续使用原安装。',
  unmanaged_mod_config_exists: '检测到非 Whale 管理的 STS2_MCP.conf，未覆盖。请先备份并移走它。',
  close_game_before_mod_changes: '游戏仍在运行，请关闭所有游戏窗口后再修改 Mod。',
  cannot_verify_game_process: '无法读取游戏进程身份，请关闭游戏后重试。',
  uninstall_previous_game_directory_first: '另一个游戏目录仍有 Whale 管理的安装，请先切回原目录卸载。',
  process_management_dependency_missing: '缺少 psutil，请重新装配插件依赖后重启 Whale。',
  github_release_unavailable: '无法访问 GitHub 发布信息，请检查网络后重试。',
  release_asset_download_failed: 'Mod 下载失败，请检查 GitHub 网络连接后重试。',
  release_asset_checksum_mismatch: '下载文件 SHA-256 校验不匹配，已拒绝安装。',
  release_asset_integrity_unavailable: '上游没有提供所需文件的 SHA-256，已拒绝安装。',
  start_ai_host_and_create_lobby_first: '请先启动 AI 房主，并在游戏中创建多人房间。',
  sts2_host_api_unavailable: 'AI 房主 API 尚未就绪。请确认已在游戏中启用 STS2_MCP，必要时重启房主窗口。',
  sts2_api_port_already_in_use: 'API 端口已被其他实例使用，请关闭已有游戏窗口。',
  stop_remaining_joiner_first: '本机加入端仍在运行，请先停止窗口再重新启动房主。',
  mod_port_not_synced: '端口设置尚未写入 Mod，请关闭游戏后点击安装 / 更新 Mod。',
  lan_download_failed: 'LanSpire 下载失败，请检查 GitHub 网络后重试。',
  lan_package_checksum_mismatch: 'LAN 发布包 SHA-256 不匹配，未安装。',
  lan_package_invalid: 'LAN 发布包内容无效，未安装。',
  lan_unmanaged_or_modified_file: '发现非 Whale 管理或被手动替换的 LanSpire 文件，未覆盖或删除。',
  lan_other_network_mod_present: '发现其他 LAN 网络 Mod，请先备份并移走冲突 Mod，避免同时修改联机协议。',
  lan_shared_custom_identity_unsupported: '共享 LAN 配置启用了自定义玩家 ID，双开可能身份冲突。请先在 LanSpire 设置中关闭自定义 ID，再关闭游戏重新配置。',
  lan_config_invalid: '现有 LanSpire 配置损坏，未覆盖。请先备份并修复该文件。',
  lan_config_not_ready: 'LAN 配置被改变，请关闭游戏后重新点击安装 / 配置 LAN Mod。',
  install_lan_before_launch: '请先点击一键配置当前模式，或安装 / 配置 LAN Mod。',
  uninstall_previous_lan_directory_first: '原游戏目录仍有受管 LAN 安装，请先切回原目录卸载。',
  lan_install_failed: 'LAN 安装失败，已尝试恢复原文件。',
  lan_install_rollback_failed: 'LAN 安装与部分恢复失败，请检查目录权限及配置备份后再继续。',
  lan_install_path_unsafe: 'LAN 安装路径包含非预期链接，未修改文件。',
  lan_appdata_unavailable: '未找到 Windows APPDATA，无法定位 LanSpire 配置。',
  no_managed_lan_installation: '当前目录没有 Whale 管理的 LAN 安装。',
  relay_prepare_first: '请先保存服务器设置，再生成配套部署包。',
  relay_host_required: '请填写服务器公网 IPv4 或域名并保存。',
  relay_files_modified: '受管中转配置被改变或丢失。请先停止中转，再重新生成并部署配套包。',
  relay_settings_changed: '服务器设置已改变，请重新生成并部署配套服务器包。',
  relay_certificate_expired: '中转证书已到期，请重新生成并部署完整服务器包。',
  relay_stop_before_changes: '请先停止中转客户端，再重新生成配置或安装。',
  relay_process_identity_unavailable: '无法确认中转进程身份，未操作其他进程。请检查系统权限。',
  relay_unmanaged_client: '中转目录存在非受管或被替换的 frpc，未覆盖。请先备份并移走它。',
  relay_package_checksum_mismatch: 'frp 发布 ZIP 的 SHA-256 不匹配，未安装。',
  relay_package_invalid: 'frp 发布包内容无效，未安装。',
  relay_download_failed: 'frp 下载或读取失败，请检查 GitHub 网络连接后重试。',
  relay_select_mode_first: '请选择 LanSpire 实验模式及服务器中转入口，并保存。',
  relay_install_first: '请先安装中转客户端。',
  relay_start_failed: '中转进程启动失败，请检查配置与目录权限。',
  relay_stop_failed: '中转尚未停止，请检查权限或稍后重试，未丢弃进程记录。',
};

/** 将错误显示为可操作的中文说明，不把成功提示与局部失败混淆。 */
function notice(message, failed = false) {
  $('notice').textContent = errors[message] || message || '';
  $('notice').className = failed ? 'error' : 'ok';
}

/** 仅经 AstrBot bridge 调用插件内 API，不访问父页面或登录凭据。 */
async function api(method, path, body = {}) {
  const result = await (method === 'GET'
    ? bridge.apiGet('page/sts2' + path, {})
    : bridge.apiPost('page/sts2' + path, body));
  if (result?.status === 'error' || result?.error) {
    throw new Error(result.message || result.error || '操作失败');
  }
  return result?.data || result;
}

/** 根据真实状态和未保存标记控制按钮，避免安装与启动并发。 */
function render() {
  const rows = state.processes || [];
  const host = rows.some((row) => row.role === 'ai_host');
  const joiner = rows.some((row) => row.role === 'human_joiner');
  for (const id of controls) $(id).disabled = busy;
  if (!dirty) {
    $('game-dir').value = state.game_dir || '';
    $('port').value = state.api_port || 15526;
    $('transport').value = state.transport || 'local';
    $('share-address').value = state.share_address || '';
    $('connection').value = state.connection || 'lan';
    $('public-port').value = state.public_port || 33771;
    $('relay-host').value = state.relay_host || '';
    $('relay-control-port').value = state.relay_control_port || 7000;
  }
  $('enabled').checked = state.enabled === true;
  $('path-state').textContent = state.game_dir_valid ? state.game_dir : '未配置 / 无效';
  $('mod-state').textContent = state.installed
    ? '已安装' + (state.installed_release ? ' · ' + state.installed_release : '') : '未安装';
  $('api-state').textContent = state.api_connected
    ? (state.game_state?.game_mode === 'multiplayer' ? '多人局在线' : 'Mod 在线，等待多人开局') : '未连接';
  $('process-state').textContent = rows.length
    ? rows.map((row) => (row.role === 'ai_host' ? 'AI 房主' : '本机加入端') + ' · PID ' + row.pid).join(' ｜ ')
    : '当前没有 Whale 管理的游戏窗口';
  $('install').disabled = busy || dirty || !state.game_dir_valid || rows.length > 0;
  $('uninstall').disabled = busy || !state.managed_install || rows.length > 0;
  $('start').disabled = busy || dirty || !state.installed || host || joiner;
  $('join').disabled = busy || dirty || !host || !state.api_connected || joiner;
  $('stop').disabled = busy || !rows.length;
  for (const id of ['game-dir', 'port', 'transport']) $(id).disabled = busy || rows.length > 0;
  $('setup').disabled = busy || rows.length > 0;
  renderLan(rows);
  renderConnection();
}

/** 呈现 LanSpire 实验状态和只读网卡候选，不将网卡地址当作公网探测结果。 */
function renderLan(rows) {
  const lan = $('transport').value === 'lan';
  $('lan-panel').hidden = !lan;
  $('local-steps').hidden = lan;
  $('lan-steps').hidden = !lan;
  $('mode-hint').textContent = lan
    ? '一键配置 STS2MCP 和固定版 LanSpire。两个窗口不带 --fastmp，必须使用游戏内 Host LAN / Join LAN。'
    : '一键定位 Steam 并配置 STS2MCP，两个窗口均带 --fastmp。此路线不提供远程朋友加入。';
  const info = state.lan || {};
  $('lan-state').textContent = info.ready ? '已配置 · ' + info.version : (errors[info.error] || '尚未配置');
  $('lan-backup').textContent = info.config_backup ? '原配置备份：' + info.config_backup : '已有配置将先备份；卸载 LAN Mod 保留共享配置和备份。';
  $('lan-install').disabled = busy || dirty || !state.game_dir_valid || rows.length > 0;
  $('lan-uninstall').disabled = busy || !info.managed_install || rows.length > 0;
  if (state.transport === 'lan') {
    $('start').disabled ||= !info.ready;
    $('join').disabled ||= !info.ready;
  }
  const addresses = state.addresses || [];
  const signature = JSON.stringify(addresses);
  if (signature !== addressSignature) {
    addressSignature = signature;
    $('address-candidate').replaceChildren(new Option('选择地址，或手动填写', ''));
    for (const row of addresses) {
      $('address-candidate').add(new Option(row.interface + ' · ' + row.address, row.address));
    }
  }
}

/** 区分本机与公网入口，并只依据已保存设置生成不含凭据的好友说明。 */
function renderConnection() {
  const connection = $('connection').value;
  const relay = state.relay || {};
  const info = state.lan || {};
  const isRelay = connection === 'relay';
  const isDirect = connection === 'direct';
  $('address-panel').hidden = isRelay;
  $('candidate-panel').hidden = connection !== 'lan';
  $('direct-guide').hidden = !isDirect;
  $('relay-panel').hidden = !isRelay;
  $('share-label').textContent = isDirect ? '房主公网 IPv4 / 域名' : '房主局域网 IPv4 / 域名';
  $('mapping-port').textContent = $('public-port').value || '33771';
  $('public-port').disabled = busy || relay.running || connection === 'lan';
  for (const id of ['connection', 'relay-host', 'relay-control-port']) $(id).disabled = busy || relay.running;
  $('transport').disabled ||= relay.running;
  $('relay-state').textContent = relay.running
    ? '客户端运行中 · 尚未确认注册/联机' : (relay.installed ? '客户端已安装 · 未运行' : '客户端未安装');
  $('relay-detail').textContent = [
    relay.prepared ? '配套配置已生成，服务端是否部署需自行确认。' : (errors[relay.error] || '尚未生成配套配置。'),
    relay.bundle_path ? '私有部署包：' + relay.bundle_path : '',
    relay.log_path ? '客户端日志：' + relay.log_path : '',
  ].filter(Boolean).join(' ');
  $('relay-prepare').disabled = busy || dirty || relay.running || !state.relay_host;
  $('relay-download').disabled = busy || dirty || !relay.prepared;
  $('relay-install').disabled = busy || relay.running || relay.installed;
  $('relay-start').disabled = busy || dirty || relay.running || !relay.installed || !relay.prepared
    || state.transport !== 'lan' || state.connection !== 'relay';
  $('relay-stop').disabled = busy || !relay.running;
  $('connection-hint').textContent = isRelay
    ? '服务器转发 UDP 游戏数据。家宽无需入站映射；frpc 进程存在不等于代理注册成功或朋友能加入。'
    : isDirect ? '公网地址由你填写并确认；支持域名，但本页不查询 DNS 或检测公网入站。'
      : '网卡候选只是本机枚举。同一局域网直接连接；已有 VPN 可使用其地址，但不是必须安装 VPN。局域网入口端口固定 33771。';
  const savedConnection = state.connection || 'lan';
  const address = savedConnection === 'relay' ? state.relay_host : state.share_address;
  const port = savedConnection === 'lan' ? 33771 : state.public_port;
  const route = savedConnection === 'relay' ? '公网服务器 UDP 中转'
    : savedConnection === 'direct' ? '公网 UDP 直连' : '局域网';
  $('invite').value = address ? [
    'STS2 三人联机：我、本机 AI 和你，入口为' + route + '。',
    '游戏版本需要一致；安装并启用 LanSpire ' + (info.version || 'v0.11.0') + '，其他游戏性 Mod 也需一致。',
    '下载：https://github.com/Madlezz/LanSpire/releases/tag/' + (info.version || 'v0.11.0'),
    savedConnection === 'lan' ? '请确保能访问房主的局域网地址；异地若使用 VPN，须已有相应路由。'
      : '直接通过互联网连接，不需要安装虚拟局域网或 frp 客户端。',
    '游戏内选择 Multiplayer → Join LAN（不要点 Steam 邀请）。',
    '加入地址：' + address + '，端口：' + port + '（UDP）。',
    '等房间中三个玩家都到齐，再选角色、准备并开始。',
    '如果连接失败，先检查版本一致，再由房主检查 UDP 映射/中转日志与防火墙规则。',
    '这是实验方案，尚未实机验收。请勿转发 MCP API 端口。',
  ].join('\n') : '请先填写当前入口地址并保存设置。服务器中转填写服务器地址，直连填写房主公网地址。';
  $('copy-invite').disabled = busy || dirty || !address || state.transport !== 'lan'
    || (savedConnection === 'relay' && !relay.prepared);
}

/** 只读刷新状态；周期刷新不覆盖正在编辑的目录和端口。 */
async function refresh() {
  if (refreshing || busy) return;
  refreshing = true;
  try {
    state = await api('GET', '');
    render();
  } catch (error) {
    notice(error.message || '状态读取失败', true);
  } finally {
    refreshing = false;
  }
}

/** 保存用户填写的目录和端口，由后端验证格式并持久化。 */
async function saveSettings() {
  const port = Number($('port').value);
  if (!Number.isInteger(port) || port < 1024 || port > 65535) {
    throw new Error('端口需为 1024 至 65535 的整数');
  }
  state = await api('POST', '/settings', {
    game_dir: $('game-dir').value.trim(), api_port: port,
    transport: $('transport').value, share_address: $('share-address').value.trim(),
    connection: $('connection').value, public_port: Number($('public-port').value),
    relay_host: $('relay-host').value.trim(), relay_control_port: Number($('relay-control-port').value),
  });
  dirty = false;
}

/** 串行执行显式点击的操作，任何失败都恢复按钮并更新实际状态。 */
async function run(action, message) {
  if (busy) return;
  busy = true;
  render();
  try {
    await action();
    notice(message);
  } catch (error) {
    notice(error.message || '操作失败', true);
  } finally {
    busy = false;
    await refresh();
    render();
  }
}

/** 一键配置尊重用户填写的目录，再让后端自动定位、校验安装和启用工具。 */
async function setup() {
  if (dirty) await saveSettings();
  state = await api('POST', '/setup');
  dirty = false;
}

for (const id of ['game-dir', 'port', 'share-address', 'transport', 'connection', 'public-port', 'relay-host', 'relay-control-port']) {
  $(id).addEventListener('input', () => { dirty = true; render(); });
}
$('refresh').onclick = refresh;
$('setup').onclick = () => run(setup, '当前模式配置完成。首次启动需允许加载对应 Mod；LAN 开房/加入仍需在游戏中操作。');
$('save').onclick = () => run(saveSettings, '设置已保存。变更端口后请点击安装 / 更新 Mod。');
$('install').onclick = () => run(() => api('POST', '/install'), 'Mod 已安装或更新。');
$('uninstall').onclick = () => run(() => api('POST', '/uninstall'), 'Whale 管理的 Mod 已卸载。');
$('start').onclick = () => run(() => api('POST', '/start'), state.transport === 'lan'
  ? 'AI 窗口已启动。请选独立档案，在游戏中使用 Host LAN 建房。' : 'AI 房主进程已启动。请在游戏中创建本机多人房间。');
$('join').onclick = () => run(() => api('POST', '/join'), state.transport === 'lan'
  ? '本机窗口已启动。请选择另一游戏档案，在 Join LAN 中填 127.0.0.1:33771。' : '本机加入端已启动，请在第二窗口选择“多人 → 加入”。');
$('lan-install').onclick = () => run(() => api('POST', '/lan/install'), 'LanSpire 已安装并配置；游戏仍需手动允许加载。');
$('lan-uninstall').onclick = () => run(() => api('POST', '/lan/uninstall'), '受管 LAN 文件已移除，共享配置与备份保留。');
/** 仅显式生成/下载操作接收私有 ZIP，不将其内容写入页面状态或邀请文案。 */
async function downloadBundle(path) {
  const result = await api('POST', path);
  if (!result.bundle_base64 || !/^[a-zA-Z0-9-]+\.zip$/.test(result.filename)) {
    throw new Error('服务端部署包返回无效');
  }
  const bytes = Uint8Array.from(atob(result.bundle_base64), (char) => char.charCodeAt(0));
  const url = URL.createObjectURL(new Blob([bytes], { type: 'application/zip' }));
  const link = document.createElement('a');
  link.href = url;
  link.download = result.filename;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
}
$('relay-prepare').onclick = () => run(() => downloadBundle('/relay/prepare'), '已生成全新配套部署包并请求下载。若未下载，可按页面路径获取；请将完整新包部署到服务器。');
$('relay-download').onclick = () => run(() => downloadBundle('/relay/download'), '已请求下载当前私有部署包，凭据未改变。');
$('relay-install').onclick = () => run(() => api('POST', '/relay/install'), '固定版本 frpc 已校验安装，尚未启动。');
$('relay-start').onclick = () => run(() => api('POST', '/relay/start'), '已请求启动中转客户端。请检查日志确认登录及代理注册；这不是 UDP 联机成功回执。');
$('relay-stop').onclick = () => run(() => api('POST', '/relay/stop'), 'Whale 管理的中转客户端已停止。');
$('address-candidate').onchange = () => {
  if (!$('address-candidate').value) return;
  $('share-address').value = $('address-candidate').value;
  dirty = true;
  render();
};
$('copy-invite').onclick = async () => {
  try {
    if (!navigator.clipboard) throw new Error('clipboard unavailable');
    await navigator.clipboard.writeText($('invite').value);
    notice('好友加入说明已复制。');
  } catch {
    $('invite').focus();
    $('invite').select();
    notice('浏览器未允许剪贴板，已选中加入说明，请按 Ctrl+C 复制。');
  }
};
$('stop').onclick = () => run(() => api('POST', '/stop'), '关闭请求已完成，请查看剩余受管进程。');
$('enabled').onchange = (event) => {
  const enabled = event.target.checked;
  run(() => api('POST', '/enabled', { enabled }), 'AI 游戏工具开关已保存。');
};

let timer;
for (const id of controls) $(id).disabled = true;
if (bridge) {
  bridge.ready().then(async () => {
    await refresh();
    timer = setInterval(refresh, 5000);
  }).catch((error) => notice(error.message || '页面桥接初始化失败', true));
} else {
  notice('请从 AstrBot 插件详情中的 STS2 联机页面打开此视图。', true);
}
window.addEventListener('beforeunload', () => clearInterval(timer));
