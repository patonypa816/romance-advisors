/**
 * 事件标准化器 —— 采集层私有协议 → 统一事件协议 的翻译层
 *
 * 核心价值：消费端永远只看到 DanmakuEvent，抖音改协议只影响本文件及解码器，
 * 消费端零改动。这是"中台基座"的根基。
 */

import type {
  DanmakuEvent,
  DanmakuUser,
  GiftEvent,
  ChatEvent,
} from '../types/events.js';
import { decodeMessageBody, longToStr, type RawProtoMessage } from '../proto/douyin.proto.js';

function ownUnsignedInteger(body: any, field: string): number | undefined {
  // protobufjs 把缺失字段的 0 放在原型上；只有线上的显式字段才算已知。
  if (!body || !Object.hasOwn(body, field)) return undefined;
  const n = Number(body[field]);
  return Number.isSafeInteger(n) && n >= 0 ? n : undefined;
}

function ownBoolean(body: any, field: string): boolean | undefined {
  return body && Object.hasOwn(body, field) && typeof body[field] === 'boolean'
    ? body[field] : undefined;
}

function toUser(body: any): DanmakuUser | undefined {
  const u = body?.user;
  if (!u) return undefined;
  const id = u.idStr || longToStr(u.id);
  if (!id) return undefined;
  return {
    id,
    nickname: u.nickName || '',
    avatar: extractAvatar(u),
    secUid: u.secUid || undefined,
    payGradeLevel: ownUnsignedInteger(u.payGrade, 'level'),
  };
}

/**
 * 提取头像 URL。
 * 优先取 avatarThumb.urlList 首个（User field 9），
 * 取不到则回退到 avatarMedium / avatarLarge。
 */
function extractAvatar(u: any): string | undefined {
  for (const field of ['avatarThumb', 'avatarMedium', 'avatarLarge']) {
    const urls = u[field]?.urlList;
    if (Array.isArray(urls) && urls.length && typeof urls[0] === 'string' && urls[0].startsWith('http')) {
      return urls[0];
    }
  }
  return undefined;
}

// 事件 roomId 统一采用采集会话的 roomId（用户可识别的 web_rid）：
// 房间管理（/api/rooms）、订阅过滤（WS/SSE 的 roomId 参数）、控制台切换均以此为准，
// 避免与消息体里的内部 webcast 房间号不一致导致过滤失配。
function createTime(body: any): number {
  const t = body?.common?.createTime;
  const n = typeof t === 'bigint' ? Number(t) : Number(t ?? 0);
  // 0 / 无效时回退到当前时间
  if (!Number.isFinite(n) || n <= 0) return Date.now();
  // 抖音的 createTime 有时是秒、有时是毫秒（同一礼物的两条消息就可能不同），统一为毫秒
  return n < 1e12 ? n * 1000 : n;
}

/**
 * 把一条抖音原始消息标准化为统一事件。
 * @returns 标准化事件；无法识别的消息返回 unknown 事件（透传），解析失败返回 null
 */
export function normalize(msg: RawProtoMessage, meta: { roomId: string }): DanmakuEvent | null {
  const decoded = decodeMessageBody(msg);
  if (!decoded) {
    // 未声明的消息类型：透传为 unknown，保留原始载荷供调试
    return {
      id: msg.msgId || `${meta.roomId}:${Date.now()}:${Math.random().toString(36).slice(2)}`,
      roomId: meta.roomId,
      platform: 'douyin',
      type: 'unknown',
      ts: Date.now(),
      receivedAt: Date.now(),
      rawMethod: msg.method,
      data: { method: msg.method },
    } satisfies DanmakuEvent;
  }

  const body = decoded.body;
  const base = {
    id: msg.msgId || decoded.msgId,
    roomId: meta.roomId,
    platform: 'douyin' as const,
    ts: createTime(body),
    receivedAt: Date.now(),
    user: toUser(body),
    rawMethod: msg.method,
  };

  switch (decoded.method) {
    case 'WebcastChatMessage': {
      const ev: ChatEvent = {
        ...base,
        type: 'chat',
        data: { content: String(body.content ?? '') },
      };
      return ev;
    }
    case 'WebcastGiftMessage': {
      const gift = body.gift;
      const iconUrls = gift?.icon?.urlList ?? gift?.image?.urlList;
      const ev: GiftEvent = {
        ...base,
        type: 'gift',
        data: {
          giftId: longToStr(body.giftId || gift?.id),
          giftName: gift?.name || '',
          giftIcon:
            Array.isArray(iconUrls) && iconUrls.length && typeof iconUrls[0] === 'string' && iconUrls[0].startsWith('http')
              ? iconUrls[0]
              : undefined,
          diamondCount: ownUnsignedInteger(gift, 'diamondCount'),
          // Do not let the generic display-time fallback make an old gift look fresh.
          sourceTimeKnown: Number.isFinite(Number(body.common?.createTime)) && Number(body.common?.createTime) > 0,
          repeatCount: ownUnsignedInteger(body, 'repeatCount'),
          comboCount: ownUnsignedInteger(body, 'comboCount'),
          groupCount: ownUnsignedInteger(body, 'groupCount'),
          repeatEnd: Object.hasOwn(body, 'repeatEnd') && [0, 1].includes(body.repeatEnd)
            ? Boolean(body.repeatEnd) : undefined,
          groupId: Object.hasOwn(body, 'groupId') ? longToStr(body.groupId) || undefined : undefined,
          combo: ownBoolean(gift, 'combo'),
          sendType: Number(body.sendType ?? 0),
          traceId: body.traceId || undefined,
        },
      };
      return ev;
    }
    case 'WebcastMemberMessage':
      return { ...base, type: 'member', data: {
        memberCount: ownUnsignedInteger(body, 'memberCount'),
        action: ownUnsignedInteger(body, 'action'),
        isSetToAdmin: ownBoolean(body, 'isSetToAdmin'),
        enterType: ownUnsignedInteger(body, 'enterType'),
      } };
    case 'WebcastLikeMessage':
      return {
        ...base,
        type: 'like',
        data: { count: Number(body.count ?? 0), total: Number(body.total ?? 0) },
      };
    case 'WebcastSocialMessage':
      return { ...base, type: 'follow', data: { action: Number(body.action ?? 0) } };
    case 'WebcastRoomUserSeqMessage':
      return {
        ...base,
        type: 'room',
        data: {
          total: Number(body.total ?? 0),
          popularity: Number(body.popularity ?? 0),
          totalUser: Number(body.totalUser ?? 0),
        },
      };
    default:
      return {
        ...base,
        type: 'unknown',
        data: { method: decoded.method },
      };
  }
}
