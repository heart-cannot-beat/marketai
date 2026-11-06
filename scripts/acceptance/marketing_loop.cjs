// Real login -> generated BFF -> domain -> PostgreSQL -> Worker -> S3.
// All business data is created through public commands. External AI/Meta are
// explicitly controlled_mock; this does not assert advertising effectiveness.
const path = require('node:path');
const fs = require('node:fs');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const root = path.resolve(__dirname, '../..');
const { chromium } = require(path.join(root,'web/node_modules/@playwright/test'));
const dir = process.env.MARKETING_ACCEPTANCE_DIR;
if (!dir) throw new Error('MARKETING_ACCEPTANCE_DIR required');
const env = JSON.parse(fs.readFileSync(path.join(dir,'web-env.json')));
const platform = JSON.parse(fs.readFileSync(path.join(dir,'platform.json')));
const base = process.env.MARKETING_ACCEPTANCE_ORIGIN || 'http://127.0.0.1:4283';
// http://localhost:4002 is the disposable docker compose stack (docker-compose.yml).
if (!['http://127.0.0.1:4283','http://localhost:4002','https://marketai-web-production.up.railway.app'].includes(base)) throw new Error('Unsupported acceptance origin');
if (base.startsWith('https:') && !platform.slug.startsWith('marketing-acceptance-')) throw new Error('Dedicated online acceptance workspace required');
const sleep = ms => new Promise(resolve=>setTimeout(resolve,ms));
async function login(browser,email) {
  const context = await browser.newContext({viewport:{width:1280,height:900}});
  const page = await context.newPage();
  await page.goto(base+'/auth/login');
  await page.locator('input[name="email"]').fill(email);
  await page.locator('input[name="password"]').fill(env.ACCEPTANCE_LOGIN_PASSWORD);
  const authenticatorSecret=env.ACCEPTANCE_TOTP_SECRETS?.[email]||env.ACCEPTANCE_TOTP_SECRET;
  if(authenticatorSecret) await page.locator('input[name="mfaCode"]').fill(require('./totp.cjs')(authenticatorSecret));
  await page.locator('button[type="submit"]').click();
  await page.waitForURL(/\/w\//,{timeout:60000});
  assert.equal(new URL(page.url()).pathname.endsWith('/experiments'),true,'Default post-login entry is marketing tests');
  return {context,page};
}
async function main() {
  if(process.argv[2]!=='verify') for(const name of ['loop-result.json','restart-result.json','runtime-evidence.json','export-validation.json']) {
    const file=path.join(dir,name);if(fs.existsSync(file))fs.unlinkSync(file);
  }
  const browser=await chromium.launch({headless:true});
  try {
    const {context,page}=await login(browser,platform.ownerEmail);
    console.log('Real credentials login succeeded');
    const csrf=(await (await context.request.get(base+'/api/csrf')).json()).token;
    const prefix=base+`/api/domain/v1/w/${platform.slug}`;
    async function api(method,url,body,revision,key=crypto.randomUUID(),expected) {
      console.log(new Date().toISOString(),method,url);
      const options={method,timeout:60000,headers:{'Origin':base,'X-CSRF-Token':csrf,
        'X-Intent-Id':key,'Accept':'application/json',...(revision?{'If-Match':`"${revision}"`}:{})},...(body!==undefined?{data:body}:{})};
      let res;
      try {res=await context.request.fetch(prefix+url,options);}
      catch {console.log('Transport outcome unknown; replaying the identical request key once');res=await context.request.fetch(prefix+url,options);}
      if([502,503,504].includes(res.status())) {console.log('Gateway unavailable; replaying identical request once');await sleep(1000);res=await context.request.fetch(prefix+url,options);}
      const result=await res.json();
      if (expected) assert.equal(res.status(),expected,JSON.stringify(result));
      else assert.ok(res.ok(),`${method} ${url}: ${res.status()} ${JSON.stringify(result)}`);
      return result;
    }
    async function poll(read,done,label) {
      for(let i=0;i<80;i++) {const result=await read();if(done(result))return result;await sleep(500);}
      throw new Error(`Timed out: ${label}`);
    }
    if(process.argv[2]==='verify') {
      const saved=JSON.parse(fs.readFileSync(path.join(dir,'loop-result.json')));
      const restored=(await api('GET',`/marketing/rounds/${saved.roundId}`)).data;
      assert.equal(restored.state,'closed');assert.equal(restored.handoff.id,saved.handoffId);
      assert.equal(restored.evaluation.input_hash,saved.evaluationHash);assert.equal(restored.explanation.id,saved.explanationId);
      assert.equal(restored.explanation.state,'succeeded');
      console.log(base.startsWith('https:')?'Fresh MFA login restored the exact closed round, export, evaluation and interpretation':'New login after process restart restored the exact closed round, export, evaluation and AI interpretation');
      fs.writeFileSync(path.join(dir,base.startsWith('https:')?'fresh-login-result.json':'restart-result.json'),JSON.stringify({passed:true,roundId:restored.id,evaluationHash:restored.evaluation.input_hash},null,2));
      return;
    }
    const identity=await api('GET','/identity');fs.writeFileSync(path.join(dir,'verified-identity.json'),JSON.stringify(identity),{mode:0o600});
    const resumeProject=process.env.MARKETING_ACCEPTANCE_RESUME_PROJECT;
    if(!resumeProject) assert.equal((await api('GET','/marketing/projects')).items.length,0,'Workspace starts without business data');
    let project;
    // Read and write the full plan (language, review and execution fields) like the web client does.
    const full='?include_languages=true&include_plan_review=true&include_brand_style=true&include_execution_plan=true';
    const readProject=()=>api('GET',`/marketing/projects/${project.id}${full}`);
    if(process.env.MARKETING_ACCEPTANCE_UI_SETUP==='1') {
      if(resumeProject) {
        project=await api('GET',`/marketing/projects/${resumeProject}`);
        assert.ok(['understanding_confirmed','plan_confirmed'].includes(project.state));assert.ok([0,5].includes(project.generation_draft_ids.length));
        await page.goto(base+`/w/${platform.slug}/experiments?test=${project.id}`);
        if(project.state!=='plan_confirmed')await page.getByLabel('Try a controlled Mock example',{exact:true}).check();
      } else {
      await page.getByLabel('Product details',{exact:true}).fill('Acceptance Charger\n65W 多口充电器，面向差旅办公用户；没有提供任何认证证明。');
      await page.getByRole('button',{name:'Save product and start',exact:true}).click();
      const list=await poll(()=>api('GET','/marketing/projects'),p=>p.items.length===1,'product saved through UI');
      project=await api('GET',`/marketing/projects/${list.items[0].id}`);
      await page.getByLabel('Try a controlled Mock example',{exact:true}).check();
      await page.getByRole('button',{name:/^Draft product understanding/}).click();
      project=await poll(readProject,p=>p.latest_call?.state==='succeeded','UI understanding');
      const facts=page.getByLabel('This fact and its evidence are accurate',{exact:true});
      await facts.first().waitFor();for(let i=0;i<await facts.count();i++)await facts.nth(i).check();
      await page.getByRole('button',{name:'Confirm understanding and continue',exact:true}).click();
      project=await poll(readProject,p=>p.state==='understanding_confirmed','UI understanding confirmation');
      }
      assert.ok(project.understanding.facts.every(f=>f.confirmation==='confirmed'));
      assert.equal(project.evidence_status,'unvalidated');
      if(project.state==='understanding_confirmed') {
        await page.getByRole('button',{name:/^Draft a test plan/}).click();
        project=await poll(readProject,p=>p.latest_call?.kind==='plan'&&p.latest_call?.state==='succeeded','UI plan');
      }
      assert.deepEqual(project.plan.angles.map(a=>a.expressions.length),[2,3]);
      if(project.state!=='plan_confirmed') {
        await page.getByRole('button',{name:'Confirm plan and prepare creatives',exact:true}).click();
        project=await poll(readProject,p=>p.state==='plan_confirmed','UI plan confirmation');
      }
      if(!project.generation_draft_ids.length) {
        await page.getByRole('combobox',{name:/^Image provider/}).selectOption('mock');
        await page.getByRole('button',{name:'Prepare generation drafts · No charge',exact:true}).click();
        project=await poll(readProject,p=>p.generation_draft_ids.length===5,'UI prepared five drafts');
      } else {
        await page.getByRole('button',{name:'Creative plan1',exact:true}).click();
      }
      console.log('Product entry, understanding, fact confirmation, dynamic plan and creative preparation all completed through the visible UI');
    } else {
    project=await api('POST','/marketing/projects',{source_text:'Acceptance Charger\n65W 多口充电器，面向差旅办公用户；没有提供任何认证证明。'});
    await api('POST',`/marketing/projects/${project.id}/planning`,{kind:'understanding',execution_mode:'mock'},project.revision);
    project=await poll(readProject,p=>p.latest_call?.state==='succeeded','understanding');
    assert.ok(project.understanding.category_need);assert.ok(project.understanding.product_choice_reason);
    project.understanding.facts.forEach(f=>f.confirmation='confirmed');
    project=await api('PATCH',`/marketing/projects/${project.id}${full}`,{command:'confirm_understanding',understanding:project.understanding},project.revision);
    assert.equal(project.evidence_status,'unvalidated');
    await api('POST',`/marketing/projects/${project.id}/planning`,{kind:'plan',execution_mode:'mock'},project.revision);
    project=await poll(readProject,p=>p.latest_call?.kind==='plan'&&p.latest_call?.state==='succeeded','plan');
    assert.deepEqual(project.plan.angles.map(a=>a.expressions.length),[2,3]);
    // One execution per combination keeps this loop's five-image evidence; plans default to two since #225.
    project=await api('PATCH',`/marketing/projects/${project.id}${full}`,{command:'confirm_plan',plan:{...project.plan,executions_per_combination:1}},project.revision);
    project=await api('POST',`/marketing/projects/${project.id}/generation-drafts${full}`,{provider:'mock',model:'mock-v1'},project.revision);
    }
    async function generate(draftId) {
      const draft=await api('GET',`/generation/drafts/${draftId}`);
      const preview=await api('POST',`/generation/drafts/${draftId}/previews`,undefined,draft.revision);
      const key=crypto.randomUUID();const body={preview_id:preview.id,preview_hash:preview.preview_hash};
      const run=await api('POST','/generation/runs',body,draft.revision,key);
      const replay=await api('POST','/generation/runs',body,draft.revision,key);assert.equal(replay.generation_run_id,run.generation_run_id);
      return run;
    }
    if(process.env.MARKETING_ACCEPTANCE_UI_SETUP==='1') {
      console.log('UI: preview saved creative and review its explicit cost');
      await page.getByRole('button',{name:'Preview and review cost',exact:true}).click();
      console.log('UI: confirm the current preview and generate');
      await page.getByRole('button',{name:'Confirm and generate',exact:true}).click();
      await poll(()=>api('GET','/generation/runs'),p=>p.items.some(r=>r.status==='succeeded'),'first generation via UI cost confirmation');
      for(let i=1;i<project.generation_draft_ids.length;i++) {
        await page.getByRole('button',{name:`Creative plan${i+1}`,exact:true}).click();
        await page.getByRole('button',{name:'Preview and review cost',exact:true}).click();
        await page.getByRole('button',{name:'Confirm and generate',exact:true}).click();
        await poll(()=>api('GET','/generation/runs'),p=>p.items.length===i+1&&p.items.every(r=>r.status==='succeeded'),`creative ${i+1} via UI`);
      }
    } else {
      for(const draft of project.generation_draft_ids) await generate(draft);
    }
    let row=(await api('POST',`/marketing/projects/${project.id}/rounds`,{start_date:'2025-01-01',end_date:'2025-01-07',
      account_timezone:'UTC',execution_mode:'mock',protocol:{min_days:7,min_impressions:1000,min_clicks:20,min_purchases:20,min_spend:'20'}},project.revision)).data;
    const readRound=async()=> (await api('GET',`/marketing/rounds/${row.id}`)).data;
    row=await poll(readRound,r=>r.assets.length===5,'five executions');
    console.log('Empty workspace -> understanding -> dynamic expressions -> confirmed plan -> five persisted generations');
    const quote=await api('GET',`/marketing/rounds/${row.id}/ai-qc/quote`);
    row=(await api('POST',`/marketing/rounds/${row.id}/ai-qc`,{quote_hash:quote.quote_hash},row.revision)).data;
    row=await poll(readRound,r=>r.assets.every(a=>a.ai_qc?.state==='succeeded'),'controlled visual QC');
    const first=row.assets[0];
    row=(await api('POST',`/marketing/rounds/${row.id}/reviews`,{asset_version_id:first.asset_version_id,quality:'needs_changes',
      checks:Object.fromEntries(Object.keys(first.required_checks).map(k=>[k,k==='proposition_clear'?'fail':'pass'])),
      note:'Controlled acceptance: make the proposition prominent; keep the comparison conditions.'},row.revision)).data;
    // Use the actual UI for correction preparation; the browser calls the real generated gateway.
    await page.goto(base+`/w/${platform.slug}/experiments?test=${project.id}`);
    // The correction form lives in that image's board drawer (#224, #230); tiles follow the board tree order.
    const board=(await api('GET',`/marketing/rounds/${row.id}?include_board=true&include_execution_plan=true&include_qc_kinds=true`)).data.board;
    const tile=board.tree.angles.flatMap(a=>a.expressions.flatMap(e=>e.combinations.flatMap(c=>c.executions))).findIndex(x=>x.asset_version_id===first.asset_version_id);
    await page.getByRole('button',{name:/ · Execution \d/}).nth(tile).click();
    const drawer=page.getByRole('dialog');await drawer.getByText('Modify with AI',{exact:true}).click();
    await drawer.getByLabel('Additional adjustments (optional)').fill('Show the travel benefit clearly');
    await drawer.getByRole('button',{name:'Prepare correction draft',exact:true}).click();
    row=await poll(readRound,r=>r.feedback_drafts.length===1,'UI correction draft');
    const correction=row.feedback_drafts[0];
    if(process.env.MARKETING_ACCEPTANCE_UI_SETUP==='1') {
      await poll(()=>page.getByLabel('Creative brief',{exact:true}).inputValue(),s=>s.includes('Show the travel benefit clearly'),'opened correction prompt contains the saved QC adjustment');
      await page.getByRole('button',{name:'Preview and review cost',exact:true}).click();
      await page.getByRole('button',{name:'Confirm and generate',exact:true}).click();
    } else await generate(correction.draft_id);
    row=await poll(readRound,r=>r.assets.length===6,'new correction version');
    const changed=row.assets.find(a=>a.asset_version_id!==first.asset_version_id&&a.combination_id===first.combination_id);
    assert.equal(changed.ready,false);assert.equal(changed.quality,null);
    const freshQuote=await api('GET',`/marketing/rounds/${row.id}/ai-qc/quote`);
    assert.ok(freshQuote.quantity>=1,'A new execution needs a fresh automatic check');
    row=(await api('POST',`/marketing/rounds/${row.id}/ai-qc`,{quote_hash:freshQuote.quote_hash},row.revision)).data;
    row=await poll(readRound,r=>r.assets.every(a=>a.ai_qc?.state==='succeeded'&&a.ai_qc.current),'fresh correction AI QC');
    const selection=row.assets.filter(a=>a.asset_version_id!==first.asset_version_id);
    for(const asset of selection) row=(await api('POST',`/marketing/rounds/${row.id}/reviews`,{asset_version_id:asset.asset_version_id,quality:'approved',
      ai_qc_id:asset.ai_qc.id,compared_asset_version_ids:asset.ai_qc.compared_asset_version_ids,
      override_note:'Controlled fixture confirmation only; no real product claims are being certified.',
      checks:Object.fromEntries(Object.keys(asset.required_checks).map(k=>[k,'pass'])),note:'Controlled mock acceptance only; not physical product or performance verification.'},row.revision)).data;
    row=(await api('POST',`/marketing/rounds/${row.id}/handoff`,{asset_version_ids:selection.map(a=>a.asset_version_id)},row.revision)).data;
    row=await poll(readRound,r=>r.handoff.state==='ready','versioned S3 export');
    const download=await api('POST',`/marketing/rounds/${row.id}/download-intent`);
    const file=await context.request.get(download.download_grant.url);assert.equal(file.status(),200);
    const bytes=await file.body();assert.equal(bytes.slice(0,2).toString(),'PK');
    fs.writeFileSync(path.join(dir,'controlled-meta-export.zip'),bytes,{mode:0o600});
    for(const [i,entry] of row.handoff.entries.entries()) row=(await api('POST',`/marketing/rounds/${row.id}/bindings`,{
      entry_id:entry.entry_id,ad_account_id:'act_123',ad_id:String(1000+i),conditions_match:true},row.revision)).data;
    let csv='date,ad_id,spend,impressions,clicks,purchases,purchase_value\n';
    for(const b of row.bindings)for(let day=1;day<=7;day++) csv+=`2025-01-0${day},${b.ad_id},${9+day},10000,100,10,300\n`;
    const metadata={filename:'controlled-meta.csv',ad_account_id:'act_123',account_timezone:'UTC',currency:'USD',attribution_window:'7d_click_1d_view',source_mode:'controlled_mock'};
    row=(await api('POST',`/marketing/rounds/${row.id}/metrics/preview`,{...metadata,csv_text:csv},row.revision)).data;
    const firstBatch=row.batches.at(-1).id;
    row=(await api('POST',`/marketing/rounds/${row.id}/metrics/confirm`,{batch_id:firstBatch},row.revision)).data;
    row=(await api('POST',`/marketing/rounds/${row.id}/evaluate`,undefined,row.revision)).data;
    assert.equal(row.evaluation.evidence_status,'simulated');assert.equal(row.evaluation.qualified_asset_version_ids.length,0);
    await page.reload();
    await page.getByRole('button',{name:'Preview simulated interpretation',exact:true}).click();
    row=await poll(readRound,r=>r.explanation?.state==='succeeded','AI interpretation via UI');
    assert.equal(row.explanation.current,true);const firstExplanation=row.explanation.id;
    row=(await api('POST',`/marketing/rounds/${row.id}/metrics/preview`,{...metadata,csv_text:csv.replaceAll(',10,10000',',11,10000'),replaces_batch_id:firstBatch},row.revision)).data;
    row=(await api('POST',`/marketing/rounds/${row.id}/metrics/confirm`,{batch_id:row.batches.at(-1).id},row.revision)).data;
    assert.equal(row.explanation.current,false);assert.equal(row.explanation.data,null);
    row=(await api('POST',`/marketing/rounds/${row.id}/evaluate`,undefined,row.revision)).data;
    row=(await api('POST',`/marketing/rounds/${row.id}/explanation`,{execution_mode:'mock'},row.revision)).data;
    row=await poll(readRound,r=>r.explanation?.state==='succeeded'&&r.explanation.id!==firstExplanation,'corrected interpretation');
    await page.reload();
    await page.getByRole('heading',{name:'AI interpretation of the evidence',exact:true}).waitFor();
    await page.setViewportSize({width:390,height:844});
    const overflow=await page.evaluate(()=>document.documentElement.scrollWidth>window.innerWidth);assert.equal(overflow,false);
    await page.screenshot({path:path.join(dir,'results-mobile.png'),fullPage:true});
    await page.setViewportSize({width:1280,height:900});await page.screenshot({path:path.join(dir,'results-desktop.png'),fullPage:true});
    row=(await api('POST',`/marketing/rounds/${row.id}/close`,{reason:'insufficient_evidence',note:'Controlled end-to-end acceptance completed. No real advertising performance claimed.'},row.revision)).data;
    const forbidden=await context.request.get(base+`/api/domain/v1/w/${platform.otherSlug}/marketing/projects/${project.id}`);assert.equal(forbidden.status(),403);
    const other=await login(browser,platform.otherEmail);
    const scoped=await other.context.request.get(base+`/api/domain/v1/w/${platform.otherSlug}/marketing/projects/${project.id}`);assert.equal(scoped.status(),404);
    await other.context.close();
    const receipt={passed:true,scope:base.startsWith('https:')?'online_real_mfa_login_bff_worker_railway_bucket_controlled_external':'local_postgresql_real_login_bff_worker_s3_controlled_external',projectId:project.id,roundId:row.id,
      handoffId:row.handoff.id,evaluationHash:row.evaluation.input_hash,explanationId:row.explanation.id,
      generatedVersions:row.assets.length,selectedVersions:row.handoff.entries.length,correctionVersion:changed.asset_version_id,
      originalVersion:first.asset_version_id,exportSha256:crypto.createHash('sha256').update(bytes).digest('hex'),
      realPaidCalls:0,realMetaWrites:0,tenantIsolation:true,mobileOverflow:false};
    fs.writeFileSync(path.join(dir,'loop-result.json'),JSON.stringify(receipt,null,2),{mode:0o600});
    console.log('QC -> UI correction -> new version -> fresh review -> S3 ZIP -> CSV -> AI interpretation -> correction invalidation -> close; tenant isolation and mobile checks passed');
  } finally {await browser.close();}
}
main().catch(e=>{console.error(String(e.message).split('Call log:')[0]);process.exitCode=1;});
