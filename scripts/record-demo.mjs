// Records the Swipeout demo (docs/swipeout-demo.*) and/or retakes the docs
// screenshots against a locally running server. Nothing on disk is modified:
// every destructive endpoint is faked through page.route() interception.
//
//   node scripts/record-demo.mjs [scanPath]     record the demo video (webm)
//   node scripts/record-demo.mjs --shots [p]    retake docs/screenshot-*.png
//
// Env: SWIPEOUT_URL (default http://127.0.0.1:8765), CHROME_PATH.
// Convert the webm afterwards with ffmpeg (see comments at the bottom).
import { chromium } from 'playwright-core';
import { existsSync, mkdirSync, readdirSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(fileURLToPath(new URL('..', import.meta.url)));
const docsDir = path.join(root, 'docs');
const artDir = path.join(root, 'artifacts');
mkdirSync(docsDir, { recursive: true });
mkdirSync(artDir, { recursive: true });

const args = process.argv.slice(2);
const shotsMode = args.includes('--shots');
const scanPath = args.find(a => !a.startsWith('--'))
  || process.env.DEMO_PATH
  || 'C:\\Users\\smaff\\Videos\\Projects';
const base = process.env.SWIPEOUT_URL || 'http://127.0.0.1:8765';

const GIB = 1024 ** 3;
const FAKE_BIN = { size: Math.round(21.4 * GIB), items: 1284 };
// Files the demo pretends are huge so they lead the deck. Basename -> GiB.
const BUMPS = {
  'fish tank tv.jpg': 2.84,
  'cow selfie.jpg': 2.13,
  'impostor.png': 1.67,
};
// Real entries are scaled down so they sit below the demo files.
const SHRINK = 0.18;

function findChrome() {
  if (process.env.CHROME_PATH && existsSync(process.env.CHROME_PATH)) return process.env.CHROME_PATH;
  const baseDir = path.join(os.homedir(), 'AppData', 'Local', 'ms-playwright');
  const hit = readdirSync(baseDir)
    .filter(n => /^chromium-\d+/.test(n))
    .sort((a, b) => Number(b.match(/\d+/)[0]) - Number(a.match(/\d+/)[0]))
    .map(n => path.join(baseDir, n, 'chrome-win64', 'chrome.exe'))
    .find(existsSync);
  if (hit) return hit;
  throw new Error('Chromium not found under ms-playwright. Set CHROME_PATH.');
}

// ---- fakery: everything destructive or inflated is rewritten here only ----
const bin = { ...FAKE_BIN };
const sizes = new Map();   // real path -> display size (bumped for the stars)
let realDrives = [];

async function installRoutes(page) {
  await page.route('**/api/**', async route => {
    const req = route.request();
    const url = new URL(req.url());
    const p = url.pathname;

    if (p === '/api/trash' && req.method() === 'POST') {
      const { path: target = '' } = req.postDataJSON() || {};
      bin.size += sizes.get(target) || 0;
      bin.items += 1;
      return route.fulfill({ json: { ok: true, bin: { ...bin }, drives: realDrives } });
    }
    if (p === '/api/empty-bin' && req.method() === 'POST') {
      return route.fulfill({ json: { ok: true, bin: { ...bin }, drives: realDrives } });
    }
    if ((p === '/api/open' || p === '/api/reveal') && req.method() === 'POST') {
      return route.fulfill({ json: { ok: true } });
    }

    const res = await route.fetch();
    if (p === '/api/info') {
      const data = await res.json();
      realDrives = data.drives || realDrives;
      data.bin = { ...bin };
      console.log('[route] info intercepted, bin ->', data.bin.size);
      return route.fulfill({ response: res, json: data });
    }
    if (p === '/api/scan/status') {
      const data = await res.json();
      const r = data.result;
      if (r && Array.isArray(r.children)) {
        for (const it of r.children) {
          const want = BUMPS[it.name];
          if (want) {
            it.size = it.logical = Math.round(want * GIB);
          } else {
            // shrink real entries so the demo files lead the deck
            it.size = it.logical = Math.round(it.size * SHRINK);
            if (it.cloudBytes) it.cloudBytes = Math.round(it.cloudBytes * SHRINK);
          }
          sizes.set(it.path, it.size);
        }
        r.total = r.children.reduce((s, c) => s + c.size, 0);
        r.children.sort((a, b) => b.size - a.size);
        console.log('[route] result rewritten, top3:', r.children.slice(0, 3).map(c => `${c.name}=${c.size}`).join(' | '));
        if (Array.isArray(r.topFiles)) {
          for (const it of r.topFiles) {
            const want = BUMPS[it.name];
            it.size = it.logical = want ? Math.round(want * GIB) : Math.round(it.size * SHRINK);
          }
          r.topFiles.sort((a, b) => b.size - a.size);
        }
      }
      return route.fulfill({ response: res, json: data });
    }
    return route.fulfill({ response: res });
  });
}

// ---- shared flow helpers ----
const errors = [];
function watch(page) {
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
  page.on('pageerror', e => errors.push(String(e)));
}

async function openTriage(page) {
  await page.fill('#pathInput', scanPath);
  await page.press('#pathInput', 'Enter');
  await page.waitForSelector('#work:not(.hidden)', { timeout: 120000 });
  await page.waitForSelector('#deck .card .preview img', { state: 'attached', timeout: 30000 });
  await page.waitForFunction(
    () => { const i = document.querySelector('#deck .card .preview img'); return i && i.complete && i.naturalWidth > 0; },
    { timeout: 30000 }
  );
}

const sleep = ms => new Promise(r => setTimeout(r, ms));

// ---- modes ----
async function record() {
  const browser = await chromium.launch({ executablePath: findChrome(), headless: true });
  const ctx = await browser.newContext({
    viewport: { width: 1280, height: 720 },
    recordVideo: { dir: artDir, size: { width: 1280, height: 720 } },
  });
  const page = await ctx.newPage();
  watch(page);
  await installRoutes(page);

  const t0 = Date.now();
  const marks = { t0 };
  await page.goto(base, { waitUntil: 'networkidle' });
  await openTriage(page);
  marks.triageVisibleMs = Date.now() - t0;

  await sleep(2200);                       // fish tank tv
  marks.bin1 = Date.now() - t0;
  await page.keyboard.press('ArrowLeft');  // bin it
  await sleep(2000);                       // cow selfie
  await page.keyboard.press('ArrowRight'); // keep it
  await sleep(2200);                       // impostor
  await page.keyboard.press('ArrowLeft');  // bin it
  await sleep(2600);                       // land on next card
  marks.end = Date.now() - t0;

  const videoPath = await page.video().path();
  await ctx.close();
  await browser.close();
  marks.video = videoPath;
  writeFileSync(path.join(artDir, 'demo-marks.json'), JSON.stringify(marks, null, 2));
  console.log(JSON.stringify({ marks, errors }, null, 2));
}

async function shots() {
  const browser = await chromium.launch({ executablePath: findChrome(), headless: true });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  watch(page);
  await installRoutes(page);

  await page.goto(base, { waitUntil: 'networkidle' });
  await sleep(400);
  await page.screenshot({ path: path.join(docsDir, 'screenshot-home.png') });

  await openTriage(page);
  await sleep(600);
  await page.screenshot({ path: path.join(docsDir, 'screenshot-triage.png') });

  await ctx.close();
  await browser.close();
  console.log(JSON.stringify({ shots: ['docs/screenshot-home.png', 'docs/screenshot-triage.png'], errors }, null, 2));
}

await (shotsMode ? shots() : record());
