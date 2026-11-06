// Production acceptance uses only the dedicated software-test workspace.
// Original and child bytes, exact-version reviews and historical evidence are checked.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '../..');
const { chromium } = require(path.join(root, 'web/node_modules/@playwright/test'));
const credentials = process.env.IMAGE_EDIT_ACCEPTANCE_CREDENTIALS;
const dir = process.env.IMAGE_EDIT_ACCEPTANCE_DIR;
if (!credentials || !dir) throw new Error('Private credentials and evidence paths are required');
const env = JSON.parse(fs.readFileSync(path.join(credentials, 'web-env.json')));
const platform = JSON.parse(fs.readFileSync(path.join(credentials, 'platform.json')));
const base = process.env.IMAGE_EDIT_ACCEPTANCE_ORIGIN || 'https://marketai-web-production.up.railway.app';
assert.ok(platform.slug.startsWith('marketing-acceptance-'));
assert.equal(base, 'https://marketai-web-production.up.railway.app');
fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
const save = (name, value) => fs.writeFileSync(path.join(dir, name), JSON.stringify(value, null, 2), {mode:0o600});
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
function historicalMeaning(value) {
 const result=JSON.parse(JSON.stringify(value));
 // Sparse responses omit optional null provenance fields during rollout.
 // Keep the manifest hash and all non-null evidence exactly comparable.
 for(const entry of result.handoff?.entries || []) {
  for(const key of Object.keys(entry.provenance || {})) {
   if(key!=='kind' && entry.provenance[key]===null)delete entry.provenance[key];
  }
 }
 return result;
}
async function main() {
 const browser = await chromium.launch({headless:true});
 try {
  const context = await browser.newContext({viewport:{width:1280,height:900}});
  const page = await context.newPage();
  const failures=[];
  page.on('pageerror', e=>failures.push(e.message));
  await page.goto(base+'/auth/login');
  await page.locator('input[name="email"]').fill(platform.ownerEmail);
  await page.locator('input[name="password"]').fill(env.ACCEPTANCE_LOGIN_PASSWORD);
  await page.locator('input[name="mfaCode"]').fill(require('./totp.cjs')(env.ACCEPTANCE_TOTP_SECRETS?.[platform.ownerEmail]||env.ACCEPTANCE_TOTP_SECRET));
  await page.locator('button[type="submit"]').click();
  await page.waitForURL(/\/w\//,{timeout:60000});
  const csrf=(await (await context.request.get(base+'/api/csrf')).json()).token;
  const prefix=base+`/api/domain/v1/w/${platform.slug}`;
  async function api(method,url,body,revision,key=crypto.randomUUID(),expected) {
   const options={method,timeout:60000,headers:{Origin:base,'X-CSRF-Token':csrf,'X-Intent-Id':key,Accept:'application/json',...(revision?{'If-Match':`"${revision}"`}:{})},...(body!==undefined?{data:body}:{})};
   const response=await context.request.fetch(prefix+url,options);
   const result=await response.json();
   if(expected)assert.equal(response.status(),expected,JSON.stringify(result));
   else assert.ok(response.ok(),`${method} ${url}: ${response.status()} ${JSON.stringify(result)}`);
   return result;
  }
  async function poll(read,done,label) {for(let i=0;i<100;i++){const r=await read();if(done(r))return r;await sleep(1000);}throw new Error('Timed out: '+label);}
  const identity=await api('GET','/identity');
  assert.equal(identity.workspace_id,platform.workspaceId||identity.workspace_id);
  const historicalFile=process.env.IMAGE_EDIT_ACCEPTANCE_HISTORICAL;
  if(!historicalFile)throw new Error('Historical acceptance receipt is required');
  const historical=JSON.parse(fs.readFileSync(historicalFile));
  const prior=(await api('GET',`/marketing/rounds/${historical.roundId}`)).data;
  // Exclude newly optional projections; hash immutable business evidence only.
  const historicalEvidence={id:prior.id,state:prior.state,snapshot:prior.snapshot,handoff:prior.handoff,evaluation:prior.evaluation,close:prior.close};
  if(process.argv[2]==='baseline') {save('historical-before.json',historicalEvidence);save('baseline.json',{passed:true,normalLoginAndMFA:true,workspace:platform.slug});console.log('Baseline: normal MFA login and historical evidence captured');return;}
  assert.deepEqual(historicalMeaning(historicalEvidence),historicalMeaning(JSON.parse(fs.readFileSync(path.join(dir,'historical-before.json')))));
  save('historical-after.json',historicalEvidence);
  let project=await api('POST','/marketing/projects',{source_text:'Image editing software acceptance 2026-10-20. Travel charger, 65W. Controlled test data only; no certification or performance claims.'});
  save('progress.json',{projectId:project.id});
  const readProject=()=>api('GET',`/marketing/projects/${project.id}`);
  await api('POST',`/marketing/projects/${project.id}/planning`,{kind:'understanding',execution_mode:'mock'},project.revision);
  project=await poll(readProject,p=>p.latest_call?.state==='succeeded','understanding');
  project.understanding.facts.forEach(f=>f.confirmation='confirmed');
  project=await api('PATCH',`/marketing/projects/${project.id}`,{command:'confirm_understanding',understanding:project.understanding},project.revision);
  await api('POST',`/marketing/projects/${project.id}/planning`,{kind:'plan',execution_mode:'mock'},project.revision);
  project=await poll(readProject,p=>p.latest_call?.kind==='plan'&&p.latest_call?.state==='succeeded','plan');
  project=await api('PATCH',`/marketing/projects/${project.id}`,{command:'confirm_plan',plan:project.plan},project.revision);
  project=await api('POST',`/marketing/projects/${project.id}/generation-drafts`,{provider:'mock',model:'mock-v1'},project.revision);
  async function generate(draftId) {
   const draft=await api('GET',`/generation/drafts/${draftId}`);
   const preview=await api('POST',`/generation/drafts/${draftId}/previews`,undefined,draft.revision);
   const key=crypto.randomUUID(),body={preview_id:preview.id,preview_hash:preview.preview_hash};
   const run=await api('POST','/generation/runs',body,draft.revision,key);
   assert.equal((await api('POST','/generation/runs',body,draft.revision,key)).generation_run_id,run.generation_run_id);
   return run;
  }
  for(const draft of project.generation_draft_ids)await generate(draft);
  let row=(await api('POST',`/marketing/projects/${project.id}/rounds`,{start_date:'2026-10-20',end_date:'2026-10-26',account_timezone:'UTC',execution_mode:'mock',protocol:{min_days:7,min_impressions:1000,min_clicks:20,min_purchases:20,min_spend:'20'}},project.revision)).data;
  save('progress.json',{projectId:project.id,roundId:row.id});
  const readRound=async()=>(await api('GET',`/marketing/rounds/${row.id}?include_image_edits=true`)).data;
  row=await poll(readRound,r=>r.assets.length===5,'initial creatives');
  const source=row.assets[0];
  async function bytes(id){const intent=await api('POST',`/generation/asset-versions/${id}/download-intent`);const res=await context.request.get(intent.download_grant.url);assert.equal(res.status(),200);return res.body();}
  const sourceBytes=await bytes(source.asset_version_id);
  row=(await api('POST',`/marketing/rounds/${row.id}/reviews`,{asset_version_id:source.asset_version_id,quality:'approved',checks:Object.fromEntries(Object.keys(source.required_checks).map(k=>[k,'pass'])),note:'Software fixture review only; no real product claims certified.'},row.revision)).data;
  assert.ok(row.assets.find(a=>a.asset_version_id===source.asset_version_id).ready);
  await page.goto(base+`/w/${platform.slug}/experiments?test=${project.id}`);
  const localPanel=page.locator('details').filter({has:page.locator('summary').filter({hasText:/^Crop and rotate$/})}).first();
  await localPanel.locator('summary').click();
  await localPanel.getByAltText('Original image').waitFor();
  await poll(()=>localPanel.getByAltText('Original image').evaluate(img=>img.complete&&img.naturalWidth>0),Boolean,'private original preview');
  assert.ok((await localPanel.getByAltText('Original image').getAttribute('src')).startsWith('blob:'));
  await localPanel.getByLabel('Crop image',{exact:true}).check();
  await localPanel.getByLabel('Width (%)',{exact:true}).fill('50');
  await localPanel.getByRole('combobox').selectOption('90');
  await localPanel.getByRole('button',{name:'Create edited version',exact:true}).click();
  row=await poll(readRound,r=>r.image_edits.some(e=>e.state==='succeeded'),'crop and rotate worker');
  const edit=row.image_edits.find(e=>e.state==='succeeded');
  const crop=row.assets.find(a=>a.asset_version_id===edit.result_asset_version_id);
  assert.equal(crop.parent_version_id,source.asset_version_id);assert.equal(crop.quality,null);assert.equal(crop.ready,false);
  assert.deepEqual(await bytes(source.asset_version_id),sourceBytes);
  const cropBytes=await bytes(crop.asset_version_id);
  const sharp=require(path.join(root,'web/node_modules/sharp'));
  const expected=await sharp(sourceBytes).extract((()=>{const w=sourceBytes.readUInt32BE(16),h=sourceBytes.readUInt32BE(20);return{left:0,top:0,width:Math.round(w*.5),height:h};})()).rotate(90).raw().toBuffer({resolveWithObject:true});
  const actual=await sharp(cropBytes).raw().toBuffer({resolveWithObject:true});
  assert.equal(actual.info.width,expected.info.width);assert.equal(actual.info.height,expected.info.height);assert.deepEqual(actual.data,expected.data);
  row=(await api('POST',`/marketing/rounds/${row.id}/reviews`,{asset_version_id:crop.asset_version_id,quality:'approved',checks:Object.fromEntries(Object.keys(crop.required_checks).map(k=>[k,'pass'])),note:'Fresh crop fixture review only; no real product claims certified.'},row.revision)).data;
  assert.equal(row.assets.find(a=>a.asset_version_id===crop.asset_version_id).ready,true);
  await page.screenshot({path:path.join(dir,'crop-and-rotate-desktop.png'),fullPage:true});
  console.log('Crop pixels, unchanged original and fresh crop review passed');
  // Prepare image-to-image from the freshly cropped version via the native UI.
  await page.reload();
  const assetArea=page.locator('div.space-y-2').filter({has:page.locator('p').filter({hasText:`Derived from version ${source.asset_version_id}`})}).first();
  const aiPanel=assetArea.locator('details').filter({has:page.locator('summary').filter({hasText:/^Modify with AI$/})}).first();
  await aiPanel.locator('summary').click();
  await aiPanel.getByLabel('Additional adjustments (optional)').fill('Change the background to warm orange, preserving the product and the original intent.');
  await aiPanel.getByRole('button',{name:'Prepare correction draft',exact:true}).click();
  row=await poll(readRound,r=>r.feedback_drafts.some(f=>f.source_asset_version_id===crop.asset_version_id),'image-to-image draft');
  const feedback=row.feedback_drafts.find(f=>f.source_asset_version_id===crop.asset_version_id);
  await page.getByText('This draft edits the selected original image.',{exact:false}).waitFor();
  await page.getByRole('button',{name:'Preview and review cost',exact:true}).click();
  await page.getByRole('button',{name:'Confirm and generate',exact:true}).click();
  row=await poll(readRound,r=>r.assets.some(a=>a.parent_version_id===crop.asset_version_id),'image-to-image child');
  const child=row.assets.find(a=>a.parent_version_id===crop.asset_version_id);
  assert.equal(child.ready,false);assert.equal(child.quality,null);assert.equal(child.ai_qc,null);
  console.log('Image-to-image child and pending exact-version review passed');
  const detail=await api('GET',`/generation/asset-versions/${child.asset_version_id}`);save('edited-version.json',detail);
  // A fresh AI suggestion never replaces explicit human exact-version approval.
  const quote=await api('GET',`/marketing/rounds/${row.id}/ai-qc/quote`);
  const checking=new Set(quote.asset_version_ids);
  assert.ok(checking.has(child.asset_version_id));
  row=(await api('POST',`/marketing/rounds/${row.id}/ai-qc`,{quote_hash:quote.quote_hash},row.revision)).data;
  row=await poll(readRound,r=>r.assets.filter(a=>checking.has(a.asset_version_id)).every(a=>a.ai_qc?.state==='succeeded'&&a.ai_qc.current),'fresh AI checks');
  assert.equal(row.assets.find(a=>a.asset_version_id===child.asset_version_id).ready,false);
  console.log('Fresh AI checks passed without granting human approval');
  const selected=row.assets.filter(a=>![source.asset_version_id,crop.asset_version_id].includes(a.asset_version_id));
  for(const a of selected)row=(await api('POST',`/marketing/rounds/${row.id}/reviews`,{asset_version_id:a.asset_version_id,quality:'approved',ai_qc_id:a.ai_qc.id,compared_asset_version_ids:a.ai_qc.compared_asset_version_ids,override_note:'Controlled software test only. No physical-product or advertising-performance validation.',checks:Object.fromEntries(Object.keys(a.required_checks).map(k=>[k,'pass'])),note:'Fresh exact version software fixture review.'},row.revision)).data;
  assert.ok(row.assets.find(a=>a.asset_version_id===child.asset_version_id).ready);
  const final=await readRound();save('reviewed-round.json',final);
  await page.reload();await page.getByText('Modify with AI',{exact:true}).first().waitFor();
  await page.screenshot({path:path.join(dir,'fresh-review-desktop.png'),fullPage:true});
  await page.setViewportSize({width:390,height:844});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
  await page.screenshot({path:path.join(dir,'fresh-review-mobile.png'),fullPage:true});
  assert.deepEqual(failures,[]);
  save('result.json',{passed:true,projectId:project.id,roundId:row.id,sourceVersion:source.asset_version_id,cropVersion:crop.asset_version_id,aiVersion:child.asset_version_id,cropThenClockwisePixelsMatch:true,originalUnchanged:true,parentChain:true,childReviewInitiallyPending:true,cropFreshReviewApproved:true,freshAIQCDidNotApprove:true,freshExactVersionReviewApproved:true,privatePreviewUsesBlobURL:true,normalLoginAndMFA:true,historicalEvidenceUnchanged:true,realPaidCalls:0,realMetaWrites:0,provider:'controlled_mock'});
  console.log('Complete: crop -> clockwise rotation -> child -> image-to-image -> child -> fresh AI check -> exact-version approval');
 }finally{await browser.close();}
}
main().catch(e=>{console.error(e.message);process.exitCode=1;});
