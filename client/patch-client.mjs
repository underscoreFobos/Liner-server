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
