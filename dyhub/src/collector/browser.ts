/**
 * 浏览器管理 —— 采集内核的"设备层"
 *
 * 采用真实浏览器（系统 Chrome headless=new）作为采集设备：
 *  - 抖音对无头 HTTP 客户端做设备风控（DEVICE_BLOCKED），真实浏览器有完整指纹 + cookie，天然规避
 *  - 签名 / 设备指纹 / cookie 由页面自行完成，抖音改协议页面自动跟随，采集永不因逆向失效
 *  - 我们只通过 CDP 旁观 WebSocket 帧，不改写页面逻辑
 *
 * 单浏览器实例、单 context（共享指纹），每个直播间一个 page。
 */

import { chromium, type Browser, type BrowserContext, type Page, type CDPSession } from 'playwright-core';
import { existsSync } from 'node:fs';
import * as cookieStore from './cookieStore.js';

const DEFAULT_CHROME_PATHS = [
  // macOS
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  '/Applications/Chromium.app/Contents/MacOS/Chromium',
  // Linux
  '/usr/bin/google-chrome',
  '/usr/bin/chromium',
  '/usr/bin/chromium-browser',
  // Windows
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
];

const FALLBACK_HEADLESS_SHELL =
  process.env.HOME + '/Library/Caches/ms-playwright/chromium_headless_shell-1234/chrome-headless-shell-mac-arm64/chrome-headless-shell';

export interface BrowserManagerOptions {
  /** Chrome 可执行文件路径，缺省时自动探测 */
  executablePath?: string;
  /** 浏览器 UA，缺省用默认桌面 UA */
  userAgent?: string;
  /** 是否无头模式 */
  headless?: boolean;
}

export interface OpenedRoom {
  page: Page;
  cdp: CDPSession;
  /** 独立 context（每房间 cookie 时创建，关房时需清理） */
  context?: BrowserContext;
}

export class BrowserManager {
  private browser: Browser | null = null;
  private context: BrowserContext | null = null;
  private readonly opts: Required<BrowserManagerOptions>;
  private pending: Promise<void> | null = null;

  constructor(opts: BrowserManagerOptions = {}) {
    this.opts = {
      executablePath: opts.executablePath ?? this.detectChrome() ?? '',
      userAgent:
        opts.userAgent ??
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
      headless: opts.headless ?? true,
    };
  }

  private detectChrome(): string | undefined {
    for (const p of DEFAULT_CHROME_PATHS) {
      try {
        if (existsSync(p)) return p;
      } catch {
        /* ignore */
      }
    }
    // playwright 缓存的 headless shell 兜底
    try {
      if (existsSync(FALLBACK_HEADLESS_SHELL)) return FALLBACK_HEADLESS_SHELL;
    } catch {
      /* ignore */
    }
    return undefined;
  }

  async init(): Promise<void> {
    if (this.pending) return this.pending;
    this.pending = (async () => {
      try {
        // 浏览器实例已存在但意外断开（被外部 kill / 崩溃）→ 重置后重新拉起
        if (this.browser && !this.browser.isConnected()) {
          this.browser = null;
          this.context = null;
        }
        if (this.browser) return;
        const exec = this.opts.executablePath;
        if (!exec) throw new Error('未找到 Chrome 可执行文件，请通过 executablePath 指定');
        this.browser = await chromium.launch({
          executablePath: exec,
          headless: this.opts.headless,
          // 本地验证保留 Chrome 沙箱，不隐藏自动化标记。
          chromiumSandbox: true,
          args: [
            '--autoplay-policy=no-user-gesture-required',
            '--disable-dev-shm-usage',
            '--mute-audio',
          ],
        });
        this.context = await this.browser.newContext({
          userAgent: this.opts.userAgent,
          viewport: { width: 1440, height: 900 },
          locale: 'zh-CN',
        });
      } catch (e) {
        // 启动失败也要清掉 pending，允许下次重试
        this.pending = null;
        throw e;
      }
    })();
    await this.pending;
  }

  /**
   * 打开一个直播间页面并建立 CDP 会话
   * 无头模式需要一次点击手势触发播放器初始化，弹幕 wss 才会建立。
   * opts.cookie：传入时创建独立 BrowserContext 注入该 cookie（多账号多房间互不干扰）；
   *               不传时用共享 context + 全局 cookieStore。
   */
  async openRoom(roomUrl: string, opts: { clickTrigger?: boolean; cookie?: string } = {}): Promise<OpenedRoom> {
    await this.init();

    let ctx: BrowserContext;
    let ownContext = false;
    if (opts.cookie && opts.cookie.trim()) {
      // 每房间独立 context + 独立 cookie
      ctx = await this.browser!.newContext({
        userAgent: this.opts.userAgent,
        viewport: { width: 1440, height: 900 },
        locale: 'zh-CN',
      });
      const cookies = opts.cookie
        .split(';')
        .map((part) => {
          const i = part.indexOf('=');
          if (i <= 0) return null;
          return {
            name: part.slice(0, i).trim(),
            value: part.slice(i + 1).trim(),
            domain: '.douyin.com',
            path: '/',
          };
        })
        .filter((c): c is { name: string; value: string; domain: string; path: string } => c !== null);
      if (cookies.length) await ctx.addCookies(cookies);
      ownContext = true;
    } else {
      // 共享 context + 全局 cookieStore
      ctx = this.context!;
      const cookieStr = cookieStore.getCookieStr();
      if (cookieStr) {
        const cookies = cookieStr
          .split(';')
          .map((part) => {
            const i = part.indexOf('=');
            if (i <= 0) return null;
            return {
              name: part.slice(0, i).trim(),
              value: part.slice(i + 1).trim(),
              domain: '.douyin.com',
              path: '/',
            };
          })
          .filter((c): c is { name: string; value: string; domain: string; path: string } => c !== null);
        if (cookies.length) await ctx.addCookies(cookies);
      }
    }

    const page = await ctx.newPage();
    const cdp = await ctx.newCDPSession(page);
    await cdp.send('Network.enable');

    await page.goto(roomUrl, { waitUntil: 'domcontentloaded', timeout: 45000 });
    // 等待页面标题就绪（页面加载完成信号）
    try {
      await page.waitForFunction(() => document.title && document.title.length > 0, { timeout: 15000 });
    } catch {
      /* 标题不可得不阻塞 */
    }
    await page.waitForTimeout(2500);
    if (opts.clickTrigger ?? true) {
      try {
        await page.mouse.click(720, 450);
      } catch {
        /* 点击失败不阻塞 */
      }
    }
    return { page, cdp, context: ownContext ? ctx : undefined };
  }

  async closeRoom(page: Page, context?: BrowserContext): Promise<void> {
    try {
      await page.close();
    } catch {
      /* 页面已关闭 */
    }
    // 独立 context（每房间 cookie）关闭后清理
    if (context) {
      try {
        await context.close();
      } catch {
        /* ignore */
      }
    }
  }

  async close(): Promise<void> {
    try {
      await this.browser?.close();
    } catch {
      /* ignore */
    }
    this.browser = null;
    this.context = null;
    this.pending = null;
  }
}
