import fs from "node:fs";
import path from "node:path";

const root = path.resolve(process.argv[2] || ".");
const apiUrl = (process.argv[3] || "http://127.0.0.1:1337").replace(/\/+$/, "");

function patch(file, edits) {
  const full = path.join(root, file);
  let text = fs.readFileSync(full, "utf8");
  const eol = text.includes("\r\n") ? "\r\n" : "\n";
  for (let [from, to] of edits) {
    from = from.replace(/\n/g, eol);
    to = to.replace(/\n/g, eol);
    if (text.includes(to) && !text.includes(from)) continue;
    if (!text.includes(from)) throw new Error(`${file}: pattern not found: ${from.slice(0, 80)}`);
    text = text.replace(from, to);
  }
  fs.writeFileSync(full, text);
  console.log(`patched ${file}`);
}

patch("electron/main.ts", [
  [
    `    mainWindow.loadFile(bundleManager.getEffectiveIndexPath());`,
    `    mainWindow.loadFile(path.join(RENDERER_DIST, "index.html"));`,
  ],
  [
    `    if (app.isPackaged) {
      setTimeout(() => {
        autoUpdater.checkForUpdates()`,
    `    if (app.isPackaged && process.env.LINER_ENABLE_UPDATES === "1") {
      setTimeout(() => {
        autoUpdater.checkForUpdates()`,
  ],
]);

patch("src/shared/api/baseUrl.ts", [
  [
    `export function switchToFallbackEdge(): boolean {
  if (currentBaseUrl === DEFAULT_PRIMARY_API) {`,
    `export function switchToFallbackEdge(): boolean {
  if (isExplicitCustomOverride(import.meta.env.VITE_API_URL as string | undefined)) return false;
  if (currentBaseUrl === DEFAULT_PRIMARY_API) {`,
  ],
  [
    `export function switchToPrimaryApi(): void {
  currentBaseUrl = DEFAULT_PRIMARY_API;`,
    `export function switchToPrimaryApi(): void {
  if (isExplicitCustomOverride(import.meta.env.VITE_API_URL as string | undefined)) return;
  currentBaseUrl = DEFAULT_PRIMARY_API;`,
  ],
]);

fs.copyFileSync(path.join(path.dirname(new URL(import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1")), "discord-rpc.ts"), path.join(root, "electron", "discord-rpc.ts"));
console.log("added electron/discord-rpc.ts");

patch("electron/main.ts", [
  [
    `import { createSleepBlocker } from "./sleep-blocker.js";`,
    `import { createSleepBlocker } from "./sleep-blocker.js";
import { discordPresence } from "./discord-rpc.js";`,
  ],
  [
    `    ipcMain.handle("storage:clear-covers-cache", async () => {`,
    `    ipcMain.handle("discord:set-activity", (_event, activity) => {
      discordPresence.setActivity(activity);
    });

    ipcMain.handle("storage:clear-covers-cache", async () => {`,
  ],
]);

patch("electron/preload.ts", [
  [
    `  syncServerTime: (dateHeaderOrMs: string | number) => Promise<number>;`,
    `  syncServerTime: (dateHeaderOrMs: string | number) => Promise<number>;
  setDiscordActivity: (activity: {
    title: string;
    artist: string;
    album?: string;
    coverUrl?: string;
    durationMs: number;
    positionMs: number;
  } | null) => Promise<void>;`,
  ],
  [
    `  syncServerTime: (dateHeaderOrMs) => ipcRenderer.invoke("signer:sync-time", dateHeaderOrMs),`,
    `  syncServerTime: (dateHeaderOrMs) => ipcRenderer.invoke("signer:sync-time", dateHeaderOrMs),
  setDiscordActivity: (activity) => ipcRenderer.invoke("discord:set-activity", activity),`,
  ],
]);

patch("src/features/player/engine/playerEngine.ts", [
  [
    `  private async syncDiscordPresence(
    _state: PlayerState,
    _trackChanged: boolean = false,
  ): Promise<void> {
  }`,
    `  private discordAnchor: { at: number; positionMs: number; rate: number } | null = null;

  private async syncDiscordPresence(
    state: PlayerState,
    _trackChanged: boolean = false,
  ): Promise<void> {
    const bridge = typeof window !== "undefined" ? window.linerElectron : undefined;
    if (!bridge?.setDiscordActivity) return;
    const track = state.currentTrack;
    if (!track || state.status !== "playing") {
      this.discordAnchor = null;
      await bridge.setDiscordActivity(null).catch(() => {});
      return;
    }
    const rate = state.playbackRate || 1;
    this.discordAnchor = { at: Date.now(), positionMs: state.positionMs, rate };
    await bridge
      .setDiscordActivity({
        title: track.title,
        artist: track.artists,
        album: track.album?.title,
        coverUrl: track.coverUrl,
        durationMs: Math.round((state.durationMs || track.durationMs) / rate),
        positionMs: Math.round(state.positionMs / rate),
      })
      .catch(() => {});
  }`,
  ],
  [
    `          void this.syncDiscordPresence(state, newTrack);
          newTrack = false;
        }`,
    `          void this.syncDiscordPresence(state, newTrack);
          newTrack = false;
        } else if (state.status === "playing" && this.discordAnchor) {
          const anchor = this.discordAnchor;
          const expected = anchor.positionMs + (Date.now() - anchor.at) * anchor.rate;
          if (Math.abs(expected - state.positionMs) > 3000 || (state.playbackRate || 1) !== anchor.rate) {
            void this.syncDiscordPresence(state);
          }
        }`,
  ],
]);

const pkgPath = path.join(root, "package.json");
const pkg = JSON.parse(fs.readFileSync(pkgPath, "utf8"));
pkg.productName = "Liner Local";
pkg.build.appId = "com.local.liner";
pkg.build.productName = "Liner Local";
pkg.build.executableName = "liner-local";
pkg.build.win.executableName = "liner-local";
pkg.build.nsis.artifactName = "Liner-Local-Setup-${version}.${ext}";
pkg.build.protocols = [{ name: "Liner Local", schemes: ["liner-local"] }];
delete pkg.build.publish;
fs.writeFileSync(pkgPath, JSON.stringify(pkg, null, 2) + "\n");
console.log("patched package.json");

fs.writeFileSync(path.join(root, ".env.production.local"), `VITE_API_URL=${apiUrl}\n`);
fs.writeFileSync(path.join(root, ".env.development.local"), `VITE_API_URL=${apiUrl}\n`);
console.log(`api url set to ${apiUrl}`);
