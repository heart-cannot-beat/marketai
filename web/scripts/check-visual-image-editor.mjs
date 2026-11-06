/* Native pointer/touch checks of the actual editor modules. All transport is
 * local fixture data; this harness cannot call a supplier or a domain server.
 * Run: node scripts/check-visual-image-editor.mjs [evidence-directory]
 */
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import http from 'node:http';
import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { chromium } from '@playwright/test';
import sharp from 'sharp';
import { execFileSync } from 'node:child_process';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
const require = createRequire(import.meta.url);
const web = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

const fixture = `
import React, {useState} from 'react';
import {createRoot} from 'react-dom/client';
import {SWRConfig} from 'swr';
import {MarketingImageEdit} from './components/workspace/MarketingImageEdit';
import {MarketingImageTools} from './components/workspace/MarketingImageTools';
import {MarketingVersionHistory} from './components/workspace/MarketingVersionHistory';
const source = {asset_version_id:'source',asset_id:'asset',parent_version_id:null,created_at:'2026-10-20T01:00:00Z',edit_operation:'generated',ready:true,quality:{quality:'approved'}};
const child = {...source,asset_version_id:'child',parent_version_id:'source',created_at:'2026-10-21T01:00:00Z',edit_operation:'remove_background',ready:false,quality:null};
window.submissions=[];
function App(){const [edit,setEdit]=useState({id:'source',request:0});return <SWRConfig value={{provider:()=>new Map()}}><main className="mx-auto max-w-4xl space-y-6 p-4"><h1 className="text-xl font-semibold">Image editor · local Mock fixture</h1><MarketingVersionHistory assets={[child,source]} editable busy={false} onEdit={id=>setEdit(e=>({id,request:e.request+1}))}/><section className="space-y-4" key={edit.id}><h2>Editing {edit.id}</h2><MarketingImageEdit assetVersionId={edit.id} selected={edit.request} busy={false} edits={[]} onCreate={body=>window.submissions.push({type:'crop',body})}/><MarketingImageTools assetVersionId={edit.id} mode="mock" busy={false} onPrepare={body=>window.submissions.push({type:'tool',body})}/></section></main></SWRConfig>}
createRoot(document.getElementById('root')).render(<App/>);
`;
const stubs = {
  'next-i18next': `import messages from ${JSON.stringify(path.join(web, 'locales/en/common.json'))};export const useTranslation=()=>({t:(key,opts={})=>(messages[key]||key).replace(/\\{\\{(\\w+)\\}\\}/g,(_,k)=>String(opts[k]??''))});`,
  './WorkspaceProvider': `export const useWorkspace=()=>({current:{workspaceId:'local-mock-editor',slug:'local'}});`,
  '@/lib/domain-client/gateway-provider': `const gateway={createGenerationAssetVersionDownloadIntent:async({versionId})=>({kind:'created',data:{download_grant:{url:versionId}}}),getGenerationCapabilities:async()=>({kind:'ok',data:{provider_options:[{id:'mock',label:'Controlled Mock',default_model:'mock-v1',available:true,execution_mode:'mock',models:[{id:'mock-v1',available:true,image_sizes:['2K'],image_tools:['upscale','remove_background','erase']}]}]}})};export const useDomainGateway=()=>gateway;`,
  '@/lib/domain-client/upload-deps': `const client={downloadBlob:async(grant)=>{const c=document.createElement('canvas');c.width=800;c.height=600;const x=c.getContext('2d');x.fillStyle='#e2e8f0';x.fillRect(0,0,800,600);x.fillStyle='#334155';x.fillRect(250,120,300,400);x.fillStyle='#fff';x.font='32px sans-serif';x.fillText('MOCK FIXTURE',280,320);if(grant.url==='child')x.clearRect(0,0,200,600);return new Promise(r=>c.toBlob(r,'image/png'));}};export const useBlobTransferClient=()=>client;`,
};

async function main() {
  const temporary = await fs.mkdtemp(
    path.join(os.tmpdir(), 'marketai-editor-browser-')
  );
  const evidence = path.resolve(process.argv[2] || temporary);
  await fs.mkdir(evidence, { recursive: true });
  let server, browser;
  try {
    const bundled = await build({
      stdin: { contents: fixture, resolveDir: web, loader: 'tsx' },
      bundle: true,
      write: false,
      jsx: 'automatic',
      define: { 'process.env.NODE_ENV': '"production"' },
      plugins: [
        {
          name: 'local-fixture-transport',
          setup(b) {
            b.onResolve(
              {
                filter:
                  /^(next-i18next|\.\/WorkspaceProvider|@\/lib\/domain-client\/(gateway-provider|upload-deps))$/,
              },
              (a) => ({ path: a.path, namespace: 'fixture' })
            );
            b.onLoad({ filter: /.*/, namespace: 'fixture' }, (a) => ({
              contents: stubs[a.path],
              loader: 'js',
              resolveDir: web,
            }));
          },
        },
      ],
    });
    await fs.writeFile(
      path.join(temporary, 'styles.css'),
      '@tailwind base;\n@tailwind components;\n@tailwind utilities;'
    );
    execFileSync(
      process.execPath,
      [
        require.resolve('tailwindcss/lib/cli.js'),
        '-i',
        path.join(temporary, 'styles.css'),
        '-o',
        path.join(temporary, 'output.css'),
        '--content',
        path.join(web, 'components/workspace/*.tsx'),
        '--minify',
      ],
      { cwd: web, stdio: 'pipe' }
    );
    const css = await fs.readFile(path.join(temporary, 'output.css'));
    server = http.createServer((req, res) => {
      if (req.url === '/bundle.js') {
        res.setHeader('content-type', 'application/javascript');
        res.end(bundled.outputFiles[0].contents);
      } else if (req.url === '/style.css') {
        res.setHeader('content-type', 'text/css');
        res.end(css);
      } else {
        res.setHeader('content-type', 'text/html');
        res.end(
          '<!doctype html><html lang="en"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Local editor check</title><link rel="stylesheet" href="/style.css"><div id="root"></div><script src="/bundle.js"></script></html>'
        );
      }
    });
    await new Promise((resolve, reject) => {
      server.once('error', reject);
      server.listen(0, '127.0.0.1', resolve);
    });
    const url = 'http://127.0.0.1:' + server.address().port;
    browser = await chromium.launch({ headless: true });
    const results = [];
    for (const mobile of [false, true]) {
      const context = await browser.newContext({
        viewport: mobile
          ? { width: 390, height: 844 }
          : { width: 1280, height: 900 },
        isMobile: mobile,
        hasTouch: mobile,
      });
      const page = await context.newPage();
      const errors = [];
      page.on('pageerror', (e) => errors.push(e.message));
      await page.route('**/*', (route) =>
        route.request().url().startsWith(url) ||
        route.request().url().startsWith('blob:')
          ? route.continue()
          : route.abort()
      );
      await page.goto(url);
      await page
        .getByText('Version history and comparison', { exact: true })
        .click();
      await page
        .getByAltText('Comparison result')
        .waitFor({ state: 'visible' });
      await page.getByRole('slider', { name: 'Show source: 50%' }).fill('0');
      const comparison = await page.getByRole('region').screenshot();
      const raw = await sharp(comparison)
        .removeAlpha()
        .raw()
        .toBuffer({ resolveWithObject: true });
      let checkerPixels = 0;
      for (let y = 5; y < raw.info.height - 5; y++) {
        for (let x = 5; x < raw.info.width / 5; x++) {
          const offset = (y * raw.info.width + x) * raw.info.channels;
          if (
            raw.data[offset] === 229 &&
            raw.data[offset + 1] === 231 &&
            raw.data[offset + 2] === 235
          )
            checkerPixels++;
        }
      }
      assert.ok(
        checkerPixels > 20,
        'transparent result must reveal the checkerboard'
      );
      await fs.writeFile(
        path.join(
          evidence,
          mobile ? 'comparison-mobile.png' : 'comparison-desktop.png'
        ),
        comparison
      );
      await page.getByRole('slider', { name: 'Show source: 0%' }).fill('25');
      await page
        .getByRole('slider', { name: 'Comparison zoom: 100%' })
        .fill('2');
      const rows = page.getByRole('listitem');
      await rows
        .first()
        .getByRole('button', { name: 'Continue editing this version' })
        .click();
      const crop = page
        .getByText('Crop and rotate', { exact: true })
        .locator('..');
      await crop.getByAltText('Original image').waitFor({ state: 'visible' });
      await crop.getByLabel('Crop image', { exact: true }).check();
      async function drag(canvas, a, b) {
        await canvas.scrollIntoViewIfNeeded();
        const box = await canvas.boundingBox();
        const from = {
          x: box.x + box.width * a.x,
          y: box.y + box.height * a.y,
        };
        const to = { x: box.x + box.width * b.x, y: box.y + box.height * b.y };
        if (mobile) {
          const cdp = await context.newCDPSession(page);
          await cdp.send('Input.dispatchTouchEvent', {
            type: 'touchStart',
            touchPoints: [{ ...from, radiusX: 1, radiusY: 1 }],
          });
          await cdp.send('Input.dispatchTouchEvent', {
            type: 'touchMove',
            touchPoints: [{ ...to, radiusX: 1, radiusY: 1 }],
          });
          await cdp.send('Input.dispatchTouchEvent', {
            type: 'touchEnd',
            touchPoints: [],
          });
          await cdp.detach();
        } else {
          await page.mouse.move(from.x, from.y);
          await page.mouse.down();
          await page.mouse.move(to.x, to.y, { steps: 4 });
          await page.mouse.up();
        }
      }
      await drag(
        crop.getByRole('group'),
        { x: 0.1, y: 0.1 },
        { x: 0.6, y: 0.6 }
      );
      await crop.getByLabel('Clockwise rotation').selectOption('90');
      await crop.getByRole('button', { name: 'Create edited version' }).click();
      const tools = page
        .getByText('Upscale, remove background or erase', { exact: true })
        .locator('..');
      await tools.locator('summary').click();
      await tools.getByLabel('Edit action').selectOption('erase');
      await tools.getByLabel('Selection tool').selectOption('brush');
      await tools.getByAltText('Original image').waitFor({ state: 'visible' });
      await drag(
        tools.getByRole('group'),
        { x: 0.2, y: 0.3 },
        { x: 0.7, y: 0.6 }
      );
      await tools.getByRole('button', { name: 'Undo stroke' }).click();
      assert.equal(
        await tools
          .getByRole('button', { name: 'Prepare edit draft' })
          .isDisabled(),
        true
      );
      await tools.getByRole('button', { name: 'Redo stroke' }).click();
      await tools.getByRole('button', { name: 'Prepare edit draft' }).click();
      const submitted = await page.evaluate(() => window.submissions);
      assert.equal(submitted.length, 2);
      assert.equal(submitted[0].body.asset_version_id, 'source');
      assert.equal(submitted[0].body.rotation, 90);
      assert.ok(Math.abs(submitted[0].body.crop.x - 0.1) < 0.01);
      assert.ok(Math.abs(submitted[0].body.crop.width - 0.5) < 0.01);
      const tool = submitted[1].body;
      assert.equal(tool.provider, 'mock');
      assert.equal(tool.model, 'mock-v1');
      assert.equal(tool.image_tool.source_width, 800);
      assert.equal(tool.image_tool.source_height, 600);
      assert.equal(tool.image_tool.strokes.length, 1);
      assert.equal(tool.image_tool.region, undefined);
      const first = tool.image_tool.strokes[0].points[0];
      assert.ok(
        Math.abs(first.x - 0.2) < 0.01 && Math.abs(first.y - 0.3) < 0.01
      );
      assert.equal(
        await page.evaluate(
          () => document.documentElement.scrollWidth > innerWidth
        ),
        false
      );
      assert.deepEqual(errors, []);
      await page.screenshot({
        path: path.join(
          evidence,
          mobile ? 'editor-mobile.png' : 'editor-desktop.png'
        ),
        fullPage: true,
      });
      results.push({
        viewport: mobile ? 'mobile-touch' : 'desktop-mouse',
        passed: true,
        submissions: submitted,
      });
      await context.close();
    }
    await fs.writeFile(
      path.join(evidence, 'browser-result.json'),
      JSON.stringify(
        {
          passed: true,
          scope:
            'Actual React modules with local fixture transport; no authenticated domain or supplier calls',
          results,
        },
        null,
        2
      ) + '\n'
    );
    process.stdout.write(
      'PASS: desktop mouse, mobile touch, crop/rotate payload, brush undo/redo, comparison and historical source selection.\n'
    );
  } finally {
    if (browser) await browser.close();
    if (server?.listening)
      await new Promise((resolve) => server.close(resolve));
    await fs.rm(temporary, { recursive: true, force: true });
  }
}
main().catch((e) => {
  process.stderr.write(String(e.stack || e) + '\n');
  process.exitCode = 1;
});
