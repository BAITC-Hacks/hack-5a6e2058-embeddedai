/* Apply the saved/system theme before styles and the application can paint. */
(() => {
  const storageKey = "money-graph-theme";
  const system = window.matchMedia("(prefers-color-scheme: dark)");
  const valid = (value) => ["light", "dark", "system"].includes(value);
  let preference = "system";
  try {
    const saved = localStorage.getItem(storageKey);
    if (valid(saved)) preference = saved;
  } catch {
    // Themes still work for this tab when browser storage is unavailable.
  }
  const apply = () => {
    const theme = preference === "system" ? (system.matches ? "dark" : "light") : preference;
    document.documentElement.dataset.theme = theme;
    document.documentElement.dataset.themePreference = preference;
    document
      .querySelector('meta[name="theme-color"]')
      ?.setAttribute("content", theme === "dark" ? "#09131d" : "#112e43");
    window.dispatchEvent(new CustomEvent("money-graph-themechange"));
  };
  window.moneyGraphTheme = {
    get preference() {
      return preference;
    },
    set(value) {
      if (!valid(value)) return;
      preference = value;
      try {
        localStorage.setItem(storageKey, preference);
      } catch {
        /* Keep the in-memory choice. */
      }
      apply();
    },
  };
  system.addEventListener("change", () => {
    if (preference === "system") apply();
  });
  window.addEventListener("storage", (event) => {
    if (event.key !== storageKey && event.key !== null) return;
    preference = valid(event.newValue) ? event.newValue : "system";
    apply();
  });
  apply();
})();
