/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        primary: {
          50: '#eff8fc',
          100: '#d8eef7',
          200: '#b7dfed',
          300: '#83c8df',
          400: '#48a9cb',
          500: '#178bb3',
          600: '#0d7397',
          700: '#105c78',
          800: '#124b60',
        },
      },
    },
  },
  plugins: [],
};
