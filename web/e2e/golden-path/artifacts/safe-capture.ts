import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import type { Page } from '@playwright/test';

const MAX_BYTES = 2 * 1024 * 1024; // 2 MB

const CAPTURE_STYLESHEET = `
  * { animation-duration: 0s !important; transition-duration: 0s !important; }
  input, textarea { caret-color: transparent !important; }
`;

type CheckpointCode =
  | 'library_setup_complete'
  | 'generation_submitted'
  | 'generation_completed'
  | 'assets_reviewed'
  | 'experiment_frozen'
  | 'experiment_launched'
  | 'experiment_inconclusive'
  | 'experiment_winner'
  | 'localization_submitted'
  | 'localization_completed'
  | 'scale_enrolled'
  | 'scale_advice'
  | 'post_restart_verify'
  | 'failure_capture';

type RouteClassification =
  | 'workspace_dashboard'
  | 'library'
  | 'generation'
  | 'assets'
  | 'experiments'
  | 'localization'
  | 'scale'
  | 'settings';

const ALLOWED_CHECKPOINTS = new Set<CheckpointCode>([
  'library_setup_complete',
  'generation_submitted',
  'generation_completed',
  'assets_reviewed',
  'experiment_frozen',
  'experiment_launched',
  'experiment_inconclusive',
  'experiment_winner',
  'localization_submitted',
  'localization_completed',
  'scale_enrolled',
  'scale_advice',
  'post_restart_verify',
  'failure_capture',
]);

const ALLOWED_ROUTES = new Set<RouteClassification>([
  'workspace_dashboard',
  'library',
  'generation',
  'assets',
  'experiments',
  'localization',
  'scale',
  'settings',
]);

const SECRET_BEARING_ROUTES = new Set<string>([
  '/invite/',
  '/auth/',
  '/signup',
  '/mfa',
]);

export interface CaptureOptions {
  checkpoint: CheckpointCode;
  route: RouteClassification;
  page: Page;
  outputDir: string;
  mask?: string[];
  failureOnly?: boolean;
  hasFailed?: boolean;
}

export interface CaptureResult {
  captured: boolean;
  path: string | null;
  reason: string;
}

function classifyRoute(url: string): RouteClassification | null {
  const pathname = new URL(url).pathname;
  if (pathname.includes('/library')) return 'library';
  if (pathname.includes('/generation')) return 'generation';
  if (pathname.includes('/assets')) return 'assets';
  if (
    pathname.includes('/experiments') ||
    pathname.includes('/legacy-experiments')
  )
    return 'experiments';
  if (pathname.includes('/localization')) return 'localization';
  if (pathname.includes('/scale')) return 'scale';
  if (pathname.includes('/settings')) return 'settings';
  if (pathname.match(/^\/w\/[^/]+\/?$/)) return 'workspace_dashboard';
  return null;
}

function isSecretBearingPage(url: string): boolean {
  const pathname = new URL(url).pathname;
  return Array.from(SECRET_BEARING_ROUTES).some((r) => pathname.includes(r));
}

function generateSafeFilename(
  checkpoint: CheckpointCode,
  index: number
): string {
  const hash = crypto.randomBytes(4).toString('hex');
  return `${checkpoint}-${index}-${hash}.png`;
}

export async function safeCapture(
  opts: CaptureOptions
): Promise<CaptureResult> {
  const { checkpoint, route, page, outputDir, mask, failureOnly, hasFailed } =
    opts;

  if (!ALLOWED_CHECKPOINTS.has(checkpoint)) {
    return {
      captured: false,
      path: null,
      reason: `EC01: unknown checkpoint '${checkpoint}'`,
    };
  }

  if (!ALLOWED_ROUTES.has(route)) {
    return {
      captured: false,
      path: null,
      reason: `EC02: unknown route '${route}'`,
    };
  }

  if (failureOnly && !hasFailed) {
    return { captured: false, path: null, reason: 'skip: no failure' };
  }

  const currentUrl = page.url();
  if (isSecretBearingPage(currentUrl)) {
    return { captured: false, path: null, reason: 'EC03: secret-bearing page' };
  }

  const actualRoute = classifyRoute(currentUrl);
  if (actualRoute !== route) {
    return {
      captured: false,
      path: null,
      reason: `EC04: route mismatch (expected ${route}, got ${actualRoute})`,
    };
  }

  const outputPath = path.resolve(outputDir);
  if (!outputPath.startsWith(path.resolve(outputDir))) {
    return { captured: false, path: null, reason: 'EC05: output path escape' };
  }

  await page.addStyleTag({ content: CAPTURE_STYLESHEET });

  const maskLocators = (mask || []).map((sel) => page.locator(sel));

  const existingFiles = fs.existsSync(outputDir)
    ? fs.readdirSync(outputDir).filter((f) => f.startsWith(checkpoint)).length
    : 0;

  const filename = generateSafeFilename(checkpoint, existingFiles);
  const filePath = path.join(outputDir, filename);

  try {
    fs.mkdirSync(outputDir, { recursive: true });

    const buffer = await page.screenshot({
      fullPage: false,
      type: 'png',
      mask: maskLocators,
      animations: 'disabled',
      caret: 'hide',
      scale: 'device',
    });

    if (buffer.length > MAX_BYTES) {
      return {
        captured: false,
        path: null,
        reason: `EC06: screenshot exceeds ${MAX_BYTES} bytes (${buffer.length})`,
      };
    }

    const stripped = stripExifFromPng(buffer);
    fs.writeFileSync(filePath, stripped);

    return { captured: true, path: filePath, reason: 'ok' };
  } catch (err) {
    return {
      captured: false,
      path: null,
      reason: `EC07: capture error: ${err instanceof Error ? err.message : String(err)}`,
    };
  }
}

function stripExifFromPng(buffer: Buffer): Buffer {
  // PNG files store metadata in tEXt/iTXt/zTXt/eXIf chunks.
  // For safety, rebuild with only critical chunks (IHDR, PLTE, IDAT, IEND).
  const SIGNATURE = Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]);
  if (!buffer.subarray(0, 8).equals(SIGNATURE)) {
    return buffer;
  }

  const criticalTypes = new Set(['IHDR', 'PLTE', 'IDAT', 'IEND']);
  const chunks: Buffer[] = [SIGNATURE];
  let offset = 8;

  while (offset < buffer.length) {
    const length = buffer.readUInt32BE(offset);
    const type = buffer.subarray(offset + 4, offset + 8).toString('ascii');
    const chunkEnd = offset + 12 + length; // 4 len + 4 type + data + 4 crc

    if (chunkEnd > buffer.length) break;

    if (criticalTypes.has(type)) {
      chunks.push(buffer.subarray(offset, chunkEnd));
    }

    offset = chunkEnd;
  }

  return Buffer.concat(chunks);
}

export type { CheckpointCode, RouteClassification };
