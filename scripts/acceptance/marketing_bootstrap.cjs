// Platform prerequisites only. No products, plans, assets or delivery data.
// Run against a disposable control database, never an existing customer DB.
const path = require('node:path');
const fs = require('node:fs');
const crypto = require('node:crypto');
const root = path.resolve(__dirname, '../..');
process.env.TS_NODE_PROJECT = path.join(root, 'scripts/tsconfig.json');
process.env.TS_NODE_TRANSPILE_ONLY = 'true';
require(path.join(root, 'web/node_modules/ts-node/register'));
const { PrismaClient } = require(path.join(root, 'web/node_modules/@prisma/client'));
const { hash } = require(path.join(root, 'web/node_modules/bcryptjs'));
const { provisionWorkspace, publishSnapshot } = require(path.join(root, 'web/lib/entitlements/publication.ts'));
const { claimAndDispatch } = require(path.join(root, 'web/lib/entitlements/dispatcher.ts'));
const { AesGcmPrivateKeyProtector } = require(path.join(root, 'web/lib/delegated-identity/key-protector.ts'));
const target = process.env.MARKETING_ACCEPTANCE_DIR;
if (!target || !process.env.DATABASE_URL?.includes('127.0.0.1:55433/marketai_control')) throw new Error('Disposable acceptance database required');
const prisma = new PrismaClient();
async function deliver() {
  const result = await claimAndDispatch(prisma, 'marketing-acceptance', {
    domainBaseUrl:'http://127.0.0.1:8817', pushToken:process.env.CONTROL_PLANE_TO_DOMAIN_ENTITLEMENT_TOKEN,
  });
  if (result.failed || result.deadLettered || result.delivered !== 2) throw new Error(`Snapshot delivery incomplete: ${JSON.stringify(result)}`);
}
async function main() {
  if (process.argv[2] === 'fresh') {
    const user = await prisma.user.findUniqueOrThrow({where:{email:'owner@acceptance.marketai.test'}});
    const other = await prisma.user.findUniqueOrThrow({where:{email:'other@acceptance.marketai.test'}});
    const plan = await prisma.planVersion.findFirstOrThrow({where:{planKey:'marketing-acceptance'}});
    const suffix=crypto.randomBytes(4).toString('hex');
    const teams=[];
    for (const [owner,slug] of [[user,`marketing-acceptance-${suffix}`],[other,`marketing-other-${suffix}`]]) {
      const team=await prisma.team.create({data:{name:slug,slug,lifecycle:'PROVISIONING'}});
      await prisma.teamMember.create({data:{userId:owner.id,teamId:team.id,role:'OWNER'}});
      await provisionWorkspace(prisma,team.id,owner.id,plan.id,'acceptance',Buffer.from(process.env.ACCEPTANCE_ENVELOPE_KEY,'hex'));
      teams.push(team);
    }
    fs.writeFileSync(path.join(target,'platform.json'),JSON.stringify({workspaceId:teams[0].id,otherWorkspaceId:teams[1].id,
      slug:teams[0].slug,otherSlug:teams[1].slug,ownerEmail:user.email,otherEmail:other.email},null,2),{mode:0o600});
    console.log('Fresh empty acceptance workspaces provisioned');
    return;
  }
  if (process.argv[2] === 'activate') {
    await deliver();
    const state = JSON.parse(fs.readFileSync(path.join(target, 'platform.json')));
    for (const teamId of [state.workspaceId, state.otherWorkspaceId]) {
      await prisma.team.update({where: {id: teamId}, data: {lifecycle: 'ACTIVE'}});
      await publishSnapshot(prisma, teamId, 'acceptance', Buffer.from(process.env.ACCEPTANCE_ENVELOPE_KEY, 'hex'));
    }
    await deliver();
    console.log('Active snapshots delivered through authenticated inbox');
    return;
  }
  if (await prisma.user.count()) throw new Error('Control database must be empty');
  const user = await prisma.user.create({data: {name: 'Marketing acceptance owner', email: 'owner@acceptance.marketai.test',
    password: await hash(process.env.ACCEPTANCE_LOGIN_PASSWORD, 12), emailVerified: new Date()}});
  const outsider = await prisma.user.create({data: {name: 'Other workspace owner', email: 'other@acceptance.marketai.test',
    password: await hash(process.env.ACCEPTANCE_LOGIN_PASSWORD, 12), emailVerified: new Date()}});
  const teams = [];
  const entitlements = {controlled_mock: true};
  const plan = await prisma.planVersion.create({data: {planKey: 'marketing-acceptance', versionNo: 1, displayName: 'Isolated acceptance only',
    entitlements, entitlementsHash: crypto.createHash('sha256').update(JSON.stringify(entitlements)).digest('hex'), publishedBy: user.id}});
  for (const [owner, slug] of [[user, 'marketing-acceptance'], [outsider, 'marketing-other']]) {
    const team = await prisma.team.create({data: {name: slug, slug, lifecycle: 'PROVISIONING'}});
    await prisma.teamMember.create({data: {userId: owner.id, teamId: team.id, role: 'OWNER'}});
    await provisionWorkspace(prisma, team.id, owner.id, plan.id, 'acceptance', Buffer.from(process.env.ACCEPTANCE_ENVELOPE_KEY, 'hex'));
    teams.push(team);
  }
  const {privateKey, publicKey} = crypto.generateKeyPairSync('ec', {namedCurve: 'prime256v1'});
  const jwk = publicKey.export({format: 'jwk'});
  const thumbprint = crypto.createHash('sha256').update(JSON.stringify({crv:jwk.crv,kty:jwk.kty,x:jwk.x,y:jwk.y})).digest('base64url');
  const kid = `acceptance-${crypto.randomUUID()}`;
  const binding = {kid, algorithm: 'ES256', publicJwkThumbprint: thumbprint};
  const protectedKey = new AesGcmPrivateKeyProtector(Buffer.from(process.env.DELEGATED_SIGNING_KEY_ENCRYPTION_KEY, 'base64'))
    .protect(privateKey.export({format:'pem',type:'pkcs8'}), binding);
  await prisma.delegatedSigningKey.create({data: {id: `dsk_${crypto.randomUUID()}`, ...binding,
    publicJwk: {...jwk,kid,alg:'ES256',use:'sig'}, protectedPrivateKey: Object.fromEntries(Object.entries(protectedKey).map(([k,v])=>[k,v.toString('base64')])),
    state: 'ACTIVE', activatedAt: new Date()}});
  fs.writeFileSync(path.join(target, 'platform.json'), JSON.stringify({workspaceId: teams[0].id, otherWorkspaceId:teams[1].id,
    slug:teams[0].slug, otherSlug:teams[1].slug, ownerEmail:user.email, otherEmail:outsider.email},null,2),{mode:0o600});
  console.log('Platform identities, signing key and provisioning snapshots prepared; no business records seeded');
}
main().finally(()=>prisma.$disconnect()).catch(e=>{console.error(e.message);process.exitCode=1;});
