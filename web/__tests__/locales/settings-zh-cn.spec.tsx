/**
 * @jest-environment jsdom
 */
import { fireEvent, render, screen } from '@testing-library/react';
import { I18nextProvider, getI18n } from 'react-i18next';
import type { ReactElement } from 'react';
import enSettings from '../../locales/en/settings.json';
import common from '../../locales/zh-CN/common.json';
import settings from '../../locales/zh-CN/settings.json';
import { CJK, readNamespace } from './locale-files';

jest.mock('@/components/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({ current: { workspaceId: 'ws_1', slug: 'ws' } }),
}));
jest.mock('@/hooks/domain/useDomainQuery', () => ({
  useDomainQuery: jest.fn(),
}));
jest.mock('@/hooks/domain/useDomainCommand', () => ({
  useDomainCommand: jest.fn(() => ({
    execute: jest.fn(),
    reset: jest.fn(),
    isPending: false,
  })),
}));

import { MetaConnectionSection } from '@/components/workspace/MetaConnectionSection';
import { ProviderCredentialsSection } from '@/components/workspace/ProviderCredentialsSection';
import { useDomainQuery } from '@/hooks/domain/useDomainQuery';

/**
 * Settings → Provider credentials and Meta Connection (#286, #298) read
 * their copy from `settings`: a zh-CN Owner sees Chinese and an en Owner sees
 * no Chinese. Provider and product names (OpenAI, Gemini, Seedream, Meta) and
 * identifiers stay as they are.
 */
const reads = (meta: unknown) =>
  jest.mocked(useDomainQuery).mockImplementation(((operationId: string) => {
    const data =
      operationId === 'list_credentials'
        ? [
            {
              credential_version_id: 'cv_1',
              credential_key: 'openai_api_key',
              version_no: 1,
              provider: 'openai',
              display_label: null,
              state: 'active',
              value_sha256: 'a1b2c3d4e5f6a7b8c9d0',
              created_at: '2026-10-01T00:00:00Z',
              rotated_at: null,
              revoked_at: null,
            },
          ]
        : operationId === 'get_generation_capabilities'
          ? { credential_readiness: {} }
          : meta;
    return { data, isLoading: false, refresh: jest.fn() };
  }) as never);

function renderIn(language: 'en' | 'zh-CN', ui: ReactElement) {
  const i18n = getI18n().cloneInstance({
    lng: language,
    fallbackLng: false,
    forkResourceStore: true,
  });
  if (language === 'zh-CN') {
    i18n.addResourceBundle('zh-CN', 'common', common);
    i18n.addResourceBundle('zh-CN', 'settings', settings);
  }
  void i18n.changeLanguage(language);
  return render(<I18nextProvider i18n={i18n}>{ui}</I18nextProvider>);
}

const settingsPage = (
  <>
    <ProviderCredentialsSection />
    <MetaConnectionSection />
  </>
);

/** en `settings` copy long enough to be a phrase rather than a name. */
const ENGLISH_COPY = Array.from(readNamespace('en', 'settings').values())
  .filter((value): value is string => typeof value === 'string')
  .map((value) => value.replace(/{{[^}]+}}/g, '').trim())
  .filter(
    (value) => /[a-z]{3,} [a-z]{2,}/i.test(value) || /^[A-Z][a-z]+$/.test(value)
  )
  .filter((value) => !/^(Meta|Google Gemini)$/.test(value));

describe('Settings sections in zh-CN and en', () => {
  it('renders provider credentials and an active Meta connection in Chinese', () => {
    reads({
      items: [
        {
          id: 'mc_1',
          state: 'active',
          meta_mode: 'customer_sandbox',
          ad_account_id: 'act_1',
        },
      ],
    });
    const { container } = renderIn('zh-CN', settingsPage);

    expect(screen.getByText('服务商凭据')).toBeInTheDocument();
    expect(screen.getByText('添加 Gemini 密钥')).toBeInTheDocument();
    expect(screen.getByText('Meta 连接')).toBeInTheDocument();
    expect(screen.getByText('已启用')).toBeInTheDocument();
    expect(screen.getByText('沙箱')).toBeInTheDocument();
    const text = container.textContent ?? '';
    expect(ENGLISH_COPY.filter((copy) => text.includes(copy))).toEqual([]);
    expect(text).not.toContain('Sandbox');
  });

  it('renders the Meta create form in Chinese', () => {
    reads({ items: [] });
    renderIn('zh-CN', <MetaConnectionSection />);
    fireEvent.click(screen.getByTestId('create-meta-connection'));
    expect(screen.getByText('证据模式')).toBeInTheDocument();
    expect(
      screen.getByRole('option', { name: '客户 Sandbox' })
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '确认' })).toBeInTheDocument();
  });

  it('has no Chinese in en', () => {
    reads({ items: [] });
    const { container } = renderIn('en', settingsPage);
    fireEvent.click(screen.getByTestId('create-meta-connection'));
    expect(
      screen.getByText(enSettings['meta-connection'].mode)
    ).toBeInTheDocument();
    expect(container.textContent).not.toMatch(CJK);
  });
});
