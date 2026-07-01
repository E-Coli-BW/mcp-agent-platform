/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  theme: {
    extend: {
      colors: {
        brand: {
          400: '#60a5fa',
          500: '#3b82f6',
          600: '#2563eb',
        },
        ide: {
          bg: '#1e1e1e',
          sidebar: '#252526',
          panel: '#2d2d2d',
          hover: '#37373d',
          border: '#3e3e42',
        },
      },
    },
  },
  plugins: [],
};
