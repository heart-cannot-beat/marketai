import en from '../../locales/en/vocabulary.json';
import zhCN from '../../locales/zh-CN/vocabulary.json';
import type { SupportedLocale } from './locales';

/**
 * Words picked by something other than the interface language, so every
 * language's copy ships at once. The namespace is a few keys and is imported
 * here rather than loaded per page by `serverSideTranslations`; the locale
 * guard tests cover it like any other namespace.
 *
 * - `language-self-name` / `language-short-name`: each language's own name
 *   (and its short form for the narrow mobile bar), so the language switcher
 *   reads the same whichever language the page is in.
 * - `plan-label-*`: labels written into a plan in the plan's strategy
 *   language (ADR 0013), not the language of the screen editing it. The server
 *   matches either language (`STYLE_LABELS` in `app/marketing/plan_review.py`).
 */
export const VOCABULARY: Record<SupportedLocale, typeof en> = {
  en,
  'zh-CN': zhCN,
};

/** Vocabulary for a plan's `strategy_language` (`zh-CN` or `en-US`). */
export const strategyVocabulary = (
  strategyLanguage: string | null | undefined
) => (strategyLanguage === 'zh-CN' ? VOCABULARY['zh-CN'] : VOCABULARY.en);
