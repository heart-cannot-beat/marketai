const typescriptEslint = require("@typescript-eslint/eslint-plugin");
const i18Next = require("eslint-plugin-i18next");
const globals = require("globals");
const tsParser = require("@typescript-eslint/parser");
const js = require("@eslint/js");

const {
    FlatCompat,
} = require("@eslint/eslintrc");

const compat = new FlatCompat({
    baseDirectory: __dirname,
    recommendedConfig: js.configs.recommended,
    allConfig: js.configs.all
});

module.exports = [{
    ignores: [
        ".next",
        ".ops",
        "**/node_modules",
        "eslint.config.cjs",
        "next-env.d.ts"
    ],
}, ...compat.extends(
    "eslint:recommended",
    "plugin:@typescript-eslint/recommended",
    "prettier",
    "next/core-web-vitals",
    "plugin:i18next/recommended",
), {
    plugins: {
        "@typescript-eslint": typescriptEslint,
        i18next: i18Next,
    },

    languageOptions: {
        globals: {
            ...globals.node,
        },

        parser: tsParser,
        ecmaVersion: 13,
        sourceType: "module",
    },
    rules: {
        "@typescript-eslint/no-explicit-any": "off",
        // Structured logs must go through lib/logger.ts, which redacts
        // cookies, tokens and provider credentials.
        "no-console": "error",
    }
}, {
    files: ["lib/logger.ts", "scripts/**/*.mjs"],

    rules: {
        "no-console": "off",
    },
}, {
    files: ["**/*.js"],

    rules: {
        "@typescript-eslint/no-require-imports": "off",
    },
}, {
    files: [
        "components/emailTemplates/**/*.tsx",
        "pages/index.tsx",
    ],

    rules: {
        "i18next/no-literal-string": "off",
    },
}];