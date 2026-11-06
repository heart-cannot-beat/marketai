import {
  Head,
  Html,
  Main,
  NextScript,
  type DocumentContext,
  type DocumentInitialProps,
} from 'next/document';
import Document from 'next/document';

type Props = DocumentInitialProps & { nonce: string };

/**
 * Carries the per-request CSP nonce minted in `middleware.ts` onto every script
 * Next.js emits, so `script-src` needs no `'unsafe-inline'` in production.
 *
 * Statically generated pages (`/404`, `/500`) are rendered at build time and
 * have no request, so no nonce attribute is emitted at all — writing `nonce=""`
 * into build output is what the Next.js CSP guide warns against. Those pages
 * still satisfy the policy: their only inline script is the `__NEXT_DATA__`
 * JSON data block, which is not executable, and every executable script is a
 * same-origin `/_next/static` source allowed by `'self'`.
 * `scripts/check-static-csp.mjs` asserts both properties against the real build
 * output.
 */
export default class MarketAIDocument extends Document<Props> {
  static async getInitialProps(ctx: DocumentContext) {
    const initialProps = await Document.getInitialProps(ctx);
    const nonce = (ctx.req?.headers['x-nonce'] as string) || '';

    return { ...initialProps, nonce };
  }

  render() {
    const { nonce, locale } = this.props;
    const nonceProp = nonce ? { nonce } : {};

    return (
      <Html lang={locale || 'en'} className="h-full" data-theme="corporate">
        <Head {...nonceProp} />
        <body className="h-full">
          <Main />
          <NextScript {...nonceProp} />
        </body>
      </Html>
    );
  }
}
