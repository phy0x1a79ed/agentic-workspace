/** The page's colour theme: the stored choice, else the system's. `styles.css` keys every colour off `data-theme`. */
export type Theme = 'dark' | 'light';

const KEY = 'scopeview.theme';

function stored(): Theme | null {
  try {
    const v = localStorage.getItem(KEY);
    return v === 'dark' || v === 'light' ? v : null;
  } catch {
    return null;
  }
}

export const theme = $state<{ current: Theme }>({
  current: stored() ?? (matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark'),
});
document.documentElement.dataset.theme = theme.current;

export function toggleTheme() {
  theme.current = theme.current === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = theme.current;
  try {
    localStorage.setItem(KEY, theme.current);
  } catch {
    // a browser that refuses storage still switches for this visit
  }
}
