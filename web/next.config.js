/* eslint @typescript-eslint/no-var-requires: "off" */
const { i18n } = require('./next-i18next.config');

/**
 * MarketAI control plane.
 *
 * Differences from upstream: the Sentry webpack wrapper is gone with the Sentry
 * dependency, and the `/.well-known/saml*` rewrites are gone with SAML. Remote
 * image patterns for boxyhq.com and files.stripe.com are removed; the control
 * plane serves only same-origin images.
 *
 * `poweredByHeader` is disabled and the static security headers below are a
 * defence-in-depth duplicate of `middleware.ts` for responses that bypass the
 * middleware matcher.
 */
/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,
  images: {
    remotePatterns: [],
  },
  i18n,
  async rewrites() {
    return [
      {
        source: '/.well-known/jwks.json',
        destination: '/api/.well-known/jwks',
      },
    ];
  },
  async headers() {
    return [
      {
        source: '/(.*?)',
        headers: [
          {
            key: 'X-Frame-Options',
            value: 'DENY',
          },
          {
            key: 'X-Content-Type-Options',
            value: 'nosniff',
          },
          {
            key: 'Referrer-Policy',
            value: 'strict-origin-when-cross-origin',
          },
        ],
      },
    ];
  },
};

module.exports = nextConfig;
