export type ThemePreference = "light" | "dark" | "system";

declare global {
  interface Window {
    moneyGraphTheme: {
      readonly preference: ThemePreference;
      set(value: ThemePreference): void;
    };
  }
}

export function initializeTheme(select: HTMLSelectElement, changed: () => void) {
  const update = () => {
    select.value = window.moneyGraphTheme.preference;
    changed();
  };
  select.onchange = () => window.moneyGraphTheme.set(select.value as ThemePreference);
  window.addEventListener("money-graph-themechange", update);
  update();
}

export function graphPalette() {
  const style = getComputedStyle(document.documentElement);
  const color = (name: string) => style.getPropertyValue(`--${name}`).trim();
  return {
    background: color("graph-background"),
    text: color("text"),
    border: color("surface"),
    seed: color("graph-seed"),
    boundary: color("graph-boundary"),
    edge: color("graph-edge"),
    arrow: color("graph-arrow"),
    selected: color("graph-selected"),
    accent: color("accent"),
  };
}
