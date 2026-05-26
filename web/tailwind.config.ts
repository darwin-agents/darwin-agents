import type { Config } from "tailwindcss";

// Tailwind v4 reads tokens from CSS @theme blocks (see app/globals.css). This
// file is kept as a stub for tooling (Prettier plugin, IDEs) that still expect
// it to exist.
const config: Config = {
  content: [
    "./app/**/*.{ts,tsx}",
    "./components/**/*.{ts,tsx}",
    "./lib/**/*.{ts,tsx}",
    "./providers/**/*.{ts,tsx}",
  ],
};

export default config;
