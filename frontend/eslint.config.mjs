// Flat-config ESLint setup for Next.js 15.
//
// Background:
//   `next lint` is deprecated and will be removed in Next.js 16. The Next.js
//   migration codemod (`@next/codemod next-lint-to-eslint-cli`) crashes on
//   repository paths that contain spaces, so this config was written by hand
//   from the official Next.js documentation template and the ESLint flat-config
//   migration guide.
//
//   The installed `eslint-config-next@15.5.12` only ships the legacy
//   (eslintrc) format. Rather than upgrading to v16 — which would change the
//   bundled rule set and could surface new warnings unrelated to this
//   migration — we bridge the existing preset through @eslint/eslintrc's
//   FlatCompat. The lint surface stays semantically identical to the previous
//   .eslintrc.json setup; only the config file format changes.
//
// When upgrading to Next.js 16:
//   - Bump `eslint-config-next` to the matching major.
//   - Replace the FlatCompat block below with the documented native flat-config
//     imports: `import nextVitals from 'eslint-config-next/core-web-vitals'`.
//   - Run the lint suite and triage any new warnings introduced by v16's
//     updated rule set.

import { FlatCompat } from "@eslint/eslintrc";
import { dirname } from "node:path";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

const compat = new FlatCompat({
    baseDirectory: __dirname,
});

const eslintConfig = [
    {
        // Match the implicit ignores from `next lint` so behavior stays the
        // same after switching to the ESLint CLI directly.
        ignores: [
            ".next/**",
            "out/**",
            "build/**",
            "next-env.d.ts",
            "node_modules/**",
        ],
    },
    ...compat.extends("next/core-web-vitals", "next/typescript"),
];

export default eslintConfig;
