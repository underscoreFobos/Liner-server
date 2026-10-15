import net from "node:net";
import fs from "node:fs";
import path from "node:path";
import { app } from "electron";

export interface DiscordActivityInput {
  title: string;
  artist: string;
  album?: string;
  coverUrl?: string;
  durationMs: number;
  positionMs: number;
}

const OP_HANDSHAKE = 0;
const OP_FRAME = 1;
const OP_CLOSE = 2;
const OP_PING = 3;
const OP_PONG = 4;
const MAX_FRAME = 64 * 1024;
const MAX_MS = 24 * 60 * 60 * 1000;

function text(value: unknown, max: number): string {
  return typeof value === "string" ? value.slice(0, max) : "";
}

function ms(value: unknown): number {
  const n = typeof value === "number" && Number.isFinite(value) ? value : 0;
  return Math.min(Math.max(0, Math.round(n)), MAX_MS);
}

export function sanitizeActivity(raw: unknown): DiscordActivityInput | null {
  if (!raw || typeof raw !== "object") return null;
  const r = raw as Record<string, unknown>;
  const cover = text(r.coverUrl, 1024);
  return {
    title: text(r.title, 256),
    artist: text(r.artist, 256),
    album: text(r.album, 256) || undefined,
    coverUrl: /^https:\/\/[^\s"]+$/.test(cover) ? cover : undefined,
    durationMs: ms(r.durationMs),
    positionMs: ms(r.positionMs),
  };
}

function pipePaths(): string[] {
  const out: string[] = [];
  for (let i = 0; i < 10; i++) {
    if (process.platform === "win32") {
      out.push(`\\\\?\\pipe\\discord-ipc-${i}`);
    } else {
      const base = process.env.XDG_RUNTIME_DIR || process.env.TMPDIR || process.env.TMP || process.env.TEMP || "/tmp";
      out.push(path.join(base, `discord-ipc-${i}`));
    }
  }
  return out;
}

function fit(value: string | undefined, fallback: string): string {
  let text = (value || "").trim() || fallback;
  if (text.length < 2) text = text + "​";
  return text.length > 128 ? text.slice(0, 127) + "…" : text;
}

class DiscordPresence {
  private socket: net.Socket | null = null;
  private ready = false;
  private connecting = false;
  private buffer = Buffer.alloc(0);
  private pending: DiscordActivityInput | null = null;
  private hasPending = false;
  private retryTimer: NodeJS.Timeout | null = null;

  private clientId(): string | null {
    const fromEnv = process.env.LINER_DISCORD_CLIENT_ID;
    if (fromEnv && /^\d{15,25}$/.test(fromEnv)) return fromEnv;
    try {
      const file = path.join(app.getPath("userData"), "discord.json");
      const parsed = JSON.parse(fs.readFileSync(file, "utf8"));
      const id = String(parsed.clientId || "");
      if (parsed.enabled === false) return null;
      return /^\d{15,25}$/.test(id) ? id : null;
    } catch {
      return null;
    }
  }

  setActivity(raw: unknown): void {
    const activity = sanitizeActivity(raw);
    this.pending = activity;
    this.hasPending = true;
    if (this.ready) {
      this.flush();
    } else if (activity) {
      this.connect();
    }
  }

  destroy(): void {
    if (this.retryTimer) clearTimeout(this.retryTimer);
    this.retryTimer = null;
    this.socket?.destroy();
    this.socket = null;
    this.ready = false;
  }

  private connect(): void {
    if (this.connecting || this.socket) return;
    const clientId = this.clientId();
    if (!clientId) return;
    this.connecting = true;
    const paths = pipePaths();

    const attempt = (index: number) => {
      if (index >= paths.length) {
        this.connecting = false;
        this.scheduleRetry();
        return;
      }
      const sock = net.createConnection(paths[index]);
      const onError = () => {
        sock.destroy();
        attempt(index + 1);
      };
      sock.once("error", onError);
      sock.once("connect", () => {
        sock.off("error", onError);
        this.connecting = false;
        this.socket = sock;
        this.buffer = Buffer.alloc(0);
        sock.on("data", (chunk) => this.onData(chunk));
        sock.on("error", () => this.reset());
        sock.on("close", () => this.reset());
        this.send(OP_HANDSHAKE, { v: 1, client_id: clientId });
      });
    };

    attempt(0);
  }

  private reset(): void {
    this.socket?.destroy();
    this.socket = null;
    this.ready = false;
    this.connecting = false;
    if (this.pending) this.scheduleRetry();
  }

  private scheduleRetry(): void {
    if (this.retryTimer || !this.pending) return;
    this.retryTimer = setTimeout(() => {
      this.retryTimer = null;
      if (this.pending) this.connect();
    }, 15000);
  }

  private send(op: number, payload: unknown): void {
    if (!this.socket) return;
    const body = Buffer.from(JSON.stringify(payload), "utf8");
    const header = Buffer.alloc(8);
    header.writeInt32LE(op, 0);
    header.writeInt32LE(body.length, 4);
    this.socket.write(Buffer.concat([header, body]));
  }

  private onData(chunk: Buffer): void {
    this.buffer = Buffer.concat([this.buffer, chunk]);
    if (this.buffer.length > MAX_FRAME + 8) {
      this.reset();
      return;
    }
    while (this.buffer.length >= 8) {
      const op = this.buffer.readInt32LE(0);
      const len = this.buffer.readInt32LE(4);
      if (len < 0 || len > MAX_FRAME) {
        this.reset();
        return;
      }
      if (this.buffer.length < 8 + len) return;
      const raw = this.buffer.subarray(8, 8 + len).toString("utf8");
      this.buffer = this.buffer.subarray(8 + len);
      let message: any = null;
      try {
        message = JSON.parse(raw);
      } catch {
        continue;
      }
      if (op === OP_PING) {
        this.send(OP_PONG, message);
      } else if (op === OP_CLOSE) {
        this.reset();
      } else if (op === OP_FRAME && message?.evt === "READY") {
        this.ready = true;
        this.flush();
      }
    }
  }

  private flush(): void {
    if (!this.ready || !this.hasPending) return;
    this.hasPending = false;
    const input = this.pending;
    let activity: Record<string, unknown> | null = null;
    if (input) {
      const now = Date.now();
      const start = now - Math.max(0, input.positionMs);
      activity = {
        type: 2,
        details: fit(input.title, "Unknown track"),
        state: fit(input.artist, "Unknown artist"),
        instance: false,
      };
      if (input.durationMs > 0) {
        activity.timestamps = { start, end: start + input.durationMs };
      }
      if (input.coverUrl) {
        activity.assets = { large_image: input.coverUrl, large_text: fit(input.album || input.title, "Liner") };
      }
    }
    this.send(OP_FRAME, {
      cmd: "SET_ACTIVITY",
      args: { pid: process.pid, activity },
      nonce: `${Date.now()}-${Math.random().toString(36).slice(2)}`,
    });
  }
}

export const discordPresence = new DiscordPresence();
