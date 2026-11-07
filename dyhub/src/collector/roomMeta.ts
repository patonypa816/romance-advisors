/**
 * 直播间主播信息解析
 *
 * 直播间 HTML 的内联数据以 HTML 实体转义（&quot; / &amp;）形式嵌入，直接正则搜不到；
 * 先做实体还原，再从 anchorInfo / roomInfo 块中提取主播昵称、头像与直播标题。
 * 仅供展示，取不到时返回 null，不影响采集主链路。
 */

const UA =
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36 Edg/140.0.0.0';

export interface RoomMeta {
  /** 主播账号名 */
  nickname: string;
  /** 主播头像 URL */
  avatar: string;
  /** 直播标题 / 简介 */
  title: string;
}

export function parseRoomMeta(html: string): RoomMeta | null {
  const clean = html
    .replace(/&quot;/g, '"')
    .replace(/&amp;/g, '&')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>');
  const nickname = clean.match(/"nickname":"([^"]{1,80})"/)?.[1] ?? '';
  const avatar = clean.match(/"avatar":"(https:[^"]+?)"/)?.[1] ?? '';
  const title = clean.match(/"title":"([^"]{1,200})"/)?.[1] ?? '';
  if (!nickname && !title) return null;
  return { nickname, avatar, title };
}

/**
 * 从直播间 HTML 解析开播状态（roomStore.roomInfo.room.status）。
 *
 * 已知状态（均已实测确认）：
 *   2 = 直播中
 *   4 = 已结束
 *
 * 其它取值（1、3、5…）的含义尚未验证，可能对应预告 / 暂停 / 审核 / 回放等场景。
 * 当前调用方统一采用白名单策略：「非 2 即不可采集」
 * （见 lightweightSession.resolveRoomId 与 LiveSession.waitForLive）。
 *
 * 若未来发现某个非 2 状态实际仍可采集，应改为黑名单策略：
 * 只把明确不可采集的状态（至少 4）视为 closed，其余放行，
 * 再由 waitForLive / 弹幕帧判断是否真正连通。调整时需同步修改两处调用方。
 *
 * 该字段在初始 HTML 的 self.__pace_f 序列化数据里，以 \"status\":N 反斜杠转义形式存在，
 * 因此需要先还原 \" 与 &quot; 两种转义再匹配。取不到时返回 null（调用方放行）。
 *
 * 匹配方式与已知脆弱点：
 *   1. 严格正则假设字段顺序为 "roomStore" → "roomInfo" → "room" → "id_str" → "status"，
 *      即 status 紧跟在 id_str 之后。抖音前端若调整 JSON 字段顺序或插入新字段，
 *      严格匹配会失配。
 *   2. 兜底正则 /"room":\{"id_str":"[^"]*","status":(\d+)/ 更宽松，但页面中可能存在
 *      多个 "room" 对象（推荐流 / 关联房间等），理论上可能匹配到错误对象。
 *
 * 解析失败（返回 null）时，调用方会跳过开播校验直接继续连接，
 * 而已下播房间的 wss 仍可能建立并推送帧，因此可能重新出现
 * 「已结束房间被误报为直播中」。若该问题复现，应优先检查抖音页面结构 / 字段顺序
 * 是否变化，并同步更新本函数及两处调用方。
 */
export function parseRoomStatus(html: string): number | null {
  const clean = html.replace(/\\"/g, '"').replace(/&quot;/g, '"');
  const m =
    clean.match(/"roomStore":\{"roomInfo":\{"room":\{"id_str":"[^"]*","status":(\d+)/) ||
    clean.match(/"room":\{"id_str":"[^"]*","status":(\d+)/);
  return m ? Number(m[1]) : null;
}

export interface LiveIdentity {
  status: 'live' | 'ended' | 'unknown';
  /** 抖音本场 webcast room ID；绝不是通常固定的网页 web_rid。 */
  liveId: string | null;
  evidence: 'roomStore.roomInfo.room' | null;
}

/** Read one JSON token without altering escapes inside nested string values. */
function jsonToken(text: string, start: number): {raw: string; end: number} | null {
  const first = text[start];
  if (!['{', '[', '"'].includes(first)) return null;
  const stack: string[] = [];
  let quoted = first === '"', escaped = false;
  if (!quoted) stack.push(first === '{' ? '}' : ']');
  for (let i = start + 1; i < text.length && i - start < 2_000_000; i++) {
    const ch = text[i];
    if (quoted) {
      if (escaped) escaped = false;
      else if (ch === '\\') escaped = true;
      else if (ch === '"') {
        quoted = false;
        if (!stack.length) return {raw: text.slice(start, i + 1), end: i + 1};
      }
    } else if (ch === '"') quoted = true;
    else if (ch === '{' || ch === '[') stack.push(ch === '{' ? '}' : ']');
    else if (ch === '}' || ch === ']') {
      if (stack.pop() !== ch) return null;
      if (!stack.length) return {raw: text.slice(start, i + 1), end: i + 1};
    }
  }
  return null;
}

/** Decode an HTML entity envelope once, never JSON's own escaping. */
function htmlEntities(text: string): string {
  return text.replace(/&quot;|&#34;|&#x22;|&lt;|&gt;|&amp;/gi, entity => ({
    '&quot;': '"', '&#34;': '"', '&#x22;': '"', '&lt;': '<', '&gt;': '>', '&amp;': '&',
  })[entity.toLowerCase()]!);
}

/**
 * Parse serialization envelopes in order: HTML/script -> push JSON array ->
 * its JSON string -> state object. A global \\" replacement corrupts valid
 * stream_data / SDK JSON strings and quoted titles, so it is never used here.
 * Only root roomStore and known state wrappers are authoritative; arbitrary
 * nested recommendation objects and their room fields are not searched.
 */
export function parseLiveIdentity(html: string, webRid: string): LiveIdentity {
  const unknown: LiveIdentity = {status: 'unknown', liveId: null, evidence: null};
  if (!/^\d{1,32}$/.test(webRid) || html.length > 8_000_000) return unknown;
  const found: LiveIdentity[] = [];
  let invalid = false;
  let budget = 32_000_000;
  const inspect = (value: unknown, depth: number): void => {
    if (depth > 8) { invalid = true; return; }
    if (value === null) return;
    if (typeof value === 'string') { parseText(value, depth + 1); return; }
    if (typeof value !== 'object' || Array.isArray(value)) return;
    const root = value as Record<string, any>;
    if (Object.hasOwn(root, 'roomStore')) {
      const room = root.roomStore?.roomInfo?.room;
      const liveId = room?.id_str;
      if (!room || typeof liveId !== 'string' || !/^\d{1,32}$/.test(liveId) || liveId === webRid
          || (room.web_rid !== undefined && String(room.web_rid) !== webRid)
          || (room.status !== 2 && room.status !== 4)) { invalid = true; return; }
      found.push({status: room.status === 2 ? 'live' : 'ended', liveId,
        evidence: 'roomStore.roomInfo.room'});
    }
    for (const key of ['state', 'initialState', 'data', 'props', 'pageProps']) {
      if (Object.hasOwn(root, key)) inspect(root[key], depth + 1);
    }
  };
  const parseText = (text: string, depth = 0): void => {
    if (depth > 8 || (budget -= text.length) < 0) { invalid = true; return; }
    text = text.trim();
    if (!text) return;
    // A complete JSON envelope is always attempted before any HTML decoding.
    try { inspect(JSON.parse(text), depth); return; } catch { /* Try known outer envelopes. */ }
    if (text.startsWith('{\\"') || text.startsWith('[\\"')) {
      try { inspect(JSON.parse('"' + text + '"'), depth + 1); return; } catch { /* Not a complete JSON-string fragment. */ }
    }
    if (text.startsWith('{&quot;') || text.startsWith('{&#34;') || text.startsWith('{&#x22;')) {
      parseText(htmlEntities(text), depth + 1); return;
    }
    // Adjacent JSON roots (or individual Flight lines) remain independently
    // parsed. Never scan for a roomStore substring inside an unknown string.
    if (/^[{["]/.test(text)) {
      let offset = 0;
      const values: unknown[] = [];
      while (offset < text.length) {
        while (/\s/.test(text[offset] ?? '') && offset < text.length) offset++;
        if (offset === text.length) break;
        const token = jsonToken(text, offset);
        if (!token) return;
        try { values.push(JSON.parse(token.raw)); } catch { return; }
        offset = token.end;
      }
      for (const value of values) inspect(value, depth + 1);
    } else if (/^\d+:/.test(text)) {
      for (const line of text.split('\n')) {
        const frame = line.match(/^\d+:(.*)$/);
        if (frame) parseText(frame[1], depth + 1);
      }
    }
  };
  parseText(html);
  const chunks: string[] = [];
  for (const script of html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script\s*>/gi)) {
    const body = script[1].trim();
    parseText(body);
    if (/^%7[bB]/.test(body)) {
      try { parseText(decodeURIComponent(body)); } catch { /* Malformed URI layer. */ }
    }
    for (const push of body.matchAll(/self\.__pace_f\.push\s*\(/g)) {
      let offset = push.index! + push[0].length;
      while (/\s/.test(body[offset] ?? '') && offset < body.length) offset++;
      const token = jsonToken(body, offset);
      if (!token || body.slice(token.end).trimStart()[0] !== ')') continue;
      try {
        const frame = JSON.parse(token.raw);
        if (Array.isArray(frame) && frame.length === 2 && frame[0] === 1 && typeof frame[1] === 'string') {
          chunks.push(frame[1]);
          parseText(frame[1]);
        }
      } catch { /* JavaScript expressions are never evaluated. */ }
    }
  }
  if (chunks.length > 1) parseText(chunks.join(''));
  if (invalid || !found.length || found.some(row => row.status !== found[0].status || row.liveId !== found[0].liveId)) return unknown;
  return found[0];
}

/** 使用既有访客 cookie 链，只在内存缓存；不读取/修改用户登录 Cookie。 */
export async function fetchLiveIdentity(roomId: string): Promise<LiveIdentity> {
  const unknown: LiveIdentity = {status: 'unknown', liveId: null, evidence: null};
  if (!/^\d{1,32}$/.test(roomId)) return unknown;
  try {
    await ensureCookies();
    const response = await fetch(`https://live.douyin.com/${roomId}`, {
      headers: {'User-Agent': UA, Cookie: cookieStr(), Referer: `https://live.douyin.com/${roomId}`},
      redirect: 'error', signal: AbortSignal.timeout(12_000),
    });
    if (!response.ok) return unknown;
    // Bound bytes while reading; response.text() would allocate the entire
    // response before its length guard could reject a large/chunked page.
    const limit = 8_000_000;
    if (Number(response.headers.get('content-length')) > limit) {
      await response.body?.cancel();
      return unknown;
    }
    const reader = response.body?.getReader();
    if (!reader) return unknown;
    let size = 0, html = '';
    const decoder = new TextDecoder();
    try {
      while (true) {
        const chunk = await reader.read();
        if (chunk.done) break;
        size += chunk.value.byteLength;
        if (size > limit) { await reader.cancel(); return unknown; }
        html += decoder.decode(chunk.value, {stream: true});
      }
      html += decoder.decode();
    } finally { reader.releaseLock(); }
    return parseLiveIdentity(html, roomId);
  } catch { return unknown; }
}

// 无 cookie 访问 live.douyin.com 会被 503 风控，会话级缓存 cookie 链（带 TTL）
let jar: Map<string, string> | null = null;
let jarAt = 0;
const JAR_TTL_MS = 15 * 60_000;
/** cookie 获取失败时的短缓存（抖音间歇限流，短 TTL 允许快速重试） */
const JAR_FAIL_TTL_MS = 30_000;

function cookieStr(): string {
  return jar ? [...jar].map(([k, v]) => `${k}=${v}`).join('; ') : '';
}

function storeCookies(headers: Headers) {
  const all = (headers as any).getSetCookie?.() ?? [];
  for (const c of all) {
    const kv = c.split(';')[0];
    const i = kv.indexOf('=');
    if (i > 0) jar!.set(kv.slice(0, i).trim(), kv.slice(i + 1));
  }
}

async function ensureCookies(): Promise<void> {
  if (jar && Date.now() - jarAt < JAR_TTL_MS) return;
  const H = { 'User-Agent': UA, Accept: 'text/html,*/*;q=0.8', 'Accept-Language': 'zh-CN,zh;q=0.9' };
  jar = new Map();
  jarAt = Date.now();
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      let r = await fetch('https://www.douyin.com/', { headers: H, signal: AbortSignal.timeout(10_000) });
      storeCookies(r.headers);
      r = await fetch('https://live.douyin.com/', {
        headers: { ...H, Cookie: cookieStr() },
        signal: AbortSignal.timeout(10_000),
      });
      storeCookies(r.headers);
      if (jar.get('ttwid')) return;
    } catch {
      // 继续重试
    }
    if (attempt < 2) await new Promise((res) => setTimeout(res, 1500 * (attempt + 1)));
  }
  // 全部失败：短缓存，允许后续快速重试
  jarAt = Date.now() - (JAR_TTL_MS - JAR_FAIL_TTL_MS);
}

/** 抓取直播间页并解析主播信息（失败返回 null，不抛错） */
export async function fetchRoomMeta(roomId: string): Promise<RoomMeta | null> {
  try {
    await ensureCookies();
    const r = await fetch(`https://live.douyin.com/${roomId}`, {
      headers: {
        'User-Agent': UA,
        Cookie: cookieStr(),
        Referer: `https://live.douyin.com/${roomId}`,
      },
      signal: AbortSignal.timeout(12_000),
    });
    if (!r.ok) return null;
    return parseRoomMeta(await r.text());
  } catch {
    return null;
  }
}
