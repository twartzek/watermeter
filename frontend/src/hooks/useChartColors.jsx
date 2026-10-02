import { useEffect, useState } from 'react';

// Chart.js draws on a canvas, so Tailwind's dark: variants don't reach it.
// The colors that need to differ between the two modes are picked here.
const LIGHT = {
  primary: '#4A5A8A',
  text: '#64748B',
  grid: '#E2E8F0',
};

const DARK = {
  primary: '#DEE4EE',
  text: '#AEB7C0',
  grid: '#3d4d60',
};

const isDark = () => window.document.body.classList.contains('dark');

// useColorMode keeps its state per component, so it can't be used to learn
// about a toggle that happened in the header. The class on <body> is the
// one shared source of truth, hence the observer.
const useChartColors = () => {
  const [dark, setDark] = useState(isDark);

  useEffect(() => {
    const observer = new MutationObserver(() => setDark(isDark()));
    observer.observe(window.document.body, {
      attributes: true,
      attributeFilter: ['class'],
    });
    setDark(isDark());
    return () => observer.disconnect();
  }, []);

  return dark ? DARK : LIGHT;
};

export default useChartColors;
