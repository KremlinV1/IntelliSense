/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{vue,ts,tsx}'],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        // VeilWatch operator-ops palette: dark slate with high-contrast
        // accents. Threat colours chosen to be distinguishable in both
        // daylight and low-light operator environments.
        ink: {
          900: '#0b0f17',
          800: '#121826',
          700: '#1b2333',
          600: '#273349',
          500: '#3a4660',
        },
        threat: {
          low: '#3b82f6',     // blue — informational
          med: '#f59e0b',     // amber — review queue
          high: '#ef4444',    // red — auto-confirmed
          critical: '#ec4899',// pink — confirmed + watchlist
        },
        ok: '#10b981',
      },
      fontFamily: {
        mono: ['JetBrains Mono', 'SF Mono', 'Menlo', 'monospace'],
      },
    },
  },
  plugins: [],
};
