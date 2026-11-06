import { createHmac } from 'node:crypto';
import type { Browser, BrowserContext, Page } from '@playwright/test';
import type { MemberSessionCapability } from '../artifacts/schemas';
import type { RuntimeSafety } from './runtime-safety';

export interface SecretInvitationCapability {
  readonly token: string;
  readonly invitationPath: string;
  readonly email: string;
}

export interface OwnerSessionCapability {
  page(): Page;
  createFreshContext(): Promise<OwnerSessionCapability>;
  close(): Promise<void>;
}

export class OnboardingHarness {
  private readonly browser: Browser;
  private readonly baseUrl: string;
  private readonly runtimeSafety: RuntimeSafety;
  private readonly contexts: BrowserContext[] = [];

  constructor(opts: {
    browser: Browser;
    baseUrl: string;
    runtimeSafety: RuntimeSafety;
  }) {
    this.browser = opts.browser;
    this.baseUrl = opts.baseUrl;
    this.runtimeSafety = opts.runtimeSafety;
  }

  async captureInvitation(
    mailSinkUrl: string,
    mailSinkToken: string,
    opts: { recipientEmail: string; timeout: number }
  ): Promise<SecretInvitationCapability> {
    const response = await fetch(`${mailSinkUrl}/api/messages/query`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${mailSinkToken}`,
      },
      body: JSON.stringify({
        recipient: opts.recipientEmail,
        timeout_ms: opts.timeout,
      }),
    });

    if (!response.ok) {
      throw new Error(`Mail sink query failed: ${response.status}`);
    }

    const data = (await response.json()) as {
      to: string;
      invitationPath: string;
    };
    const pathMatch = data.invitationPath.match(
      /\/invitations\/([A-Za-z0-9_-]+)/
    );
    if (!pathMatch) {
      throw new Error('Could not extract invitation token from path');
    }

    return {
      token: pathMatch[1],
      invitationPath: data.invitationPath,
      email: data.to,
    };
  }

  async enrollOwner(
    invitation: SecretInvitationCapability
  ): Promise<OwnerSessionCapability> {
    const password = 'GoldenPathOwner!2026';
    const context = await this.browser.newContext({
      baseURL: this.baseUrl,
      storageState: undefined,
      serviceWorkers: 'block',
    });
    this.contexts.push(context);

    await this.runtimeSafety.installNetworkSpy(context);

    const page = await context.newPage();
    await this.acceptInvitation(
      page,
      invitation,
      'Golden Path Owner',
      password
    );
    await this.signIn(page, invitation.email, password);
    await page.waitForURL(/\/settings\/security\?onboarding=/, {
      timeout: 15_000,
    });

    await page.getByRole('button', { name: 'Set up MFA' }).click();
    const totpSecret = (await page.locator('code').first().innerText()).trim();
    await page.getByLabel('Authenticator code').fill(totpCode(totpSecret));
    await page.getByRole('button', { name: 'Confirm MFA' }).click();
    await page
      .getByRole('link', { name: /Continue to Workspace/i })
      .waitFor({ state: 'visible', timeout: 15_000 });

    const capability: OwnerSessionCapability = {
      page: () => page,

      createFreshContext: async (): Promise<OwnerSessionCapability> => {
        const freshContext = await this.browser.newContext({
          baseURL: this.baseUrl,
          storageState: undefined,
          serviceWorkers: 'block',
        });
        this.contexts.push(freshContext);
        await this.runtimeSafety.installNetworkSpy(freshContext);
        const freshPage = await freshContext.newPage();
        await freshPage.goto('/auth/login');
        await this.signIn(
          freshPage,
          invitation.email,
          password,
          totpCode(totpSecret)
        );

        return {
          page: () => freshPage,
          createFreshContext: capability.createFreshContext,
          close: async () => {
            await freshPage.close();
            await freshContext.close();
          },
        };
      },

      close: async () => {
        await page.close();
        await context.close();
      },
    };

    return capability;
  }

  async enrollMember(
    invitation: SecretInvitationCapability
  ): Promise<MemberSessionCapability> {
    const password = 'GoldenPathMember!2026';
    const context = await this.browser.newContext({
      baseURL: this.baseUrl,
      storageState: undefined,
      serviceWorkers: 'block',
    });
    this.contexts.push(context);

    await this.runtimeSafety.installNetworkSpy(context);

    const page = await context.newPage();
    await this.acceptInvitation(
      page,
      invitation,
      'Golden Path Member',
      password
    );
    await this.signIn(page, invitation.email, password);

    return {
      page: () => page,
      close: async () => {
        await page.close();
        await context.close();
      },
    };
  }

  async closeAll(): Promise<void> {
    for (const ctx of this.contexts) {
      try {
        await ctx.close();
      } catch {
        /* best-effort cleanup */
      }
    }
    this.contexts.length = 0;
  }

  private async acceptInvitation(
    page: Page,
    invitation: SecretInvitationCapability,
    name: string,
    password: string
  ): Promise<void> {
    const invitationResponse = await page.request.get(
      `/api/invitations/${encodeURIComponent(invitation.token)}`
    );
    if (!invitationResponse.ok()) {
      throw new Error(
        `Invitation preflight failed with status ${invitationResponse.status()}`
      );
    }
    await page.goto(`/auth/join?token=${encodeURIComponent(invitation.token)}`);
    await page.locator('input[name="name"]').fill(name);
    await page.locator('input[name="password"]').fill(password);
    await page.getByRole('button', { name: 'Create Account' }).click();
    await page.waitForURL(/\/auth\/login/, { timeout: 15_000 });
  }

  private async signIn(
    page: Page,
    email: string,
    password: string,
    mfaCode?: string
  ): Promise<void> {
    await page.locator('input[name="email"]').fill(email);
    await page.locator('input[name="password"]').fill(password);
    if (mfaCode) {
      await page.locator('input[name="mfaCode"]').fill(mfaCode);
    }
    await page.getByRole('button', { name: /sign in/i }).click();
    await page.waitForURL((url) => !url.pathname.startsWith('/auth/login'), {
      timeout: 30_000,
    });
  }
}

function totpCode(secret: string): string {
  const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';
  let bits = '';
  for (const character of secret.replace(/\s|-/g, '').toUpperCase()) {
    const value = alphabet.indexOf(character);
    if (value < 0) throw new Error('Invalid TOTP secret');
    bits += value.toString(2).padStart(5, '0');
  }
  const bytes: number[] = [];
  for (let offset = 0; offset + 8 <= bits.length; offset += 8) {
    bytes.push(Number.parseInt(bits.slice(offset, offset + 8), 2));
  }
  const counter = Buffer.alloc(8);
  counter.writeBigUInt64BE(BigInt(Math.floor(Date.now() / 30_000)));
  const digest = createHmac('sha1', Buffer.from(bytes))
    .update(counter)
    .digest();
  const offset = digest[digest.length - 1] & 0x0f;
  return ((digest.readUInt32BE(offset) & 0x7fffffff) % 1_000_000)
    .toString()
    .padStart(6, '0');
}
