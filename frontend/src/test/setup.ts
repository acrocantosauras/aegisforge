import "@testing-library/jest-dom/vitest";

// jsdom doesn't implement matchMedia — some UI libraries query it.
if (!window.matchMedia) {
  window.matchMedia = (query: string) =>
    ({
      matches: false,
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    }) as unknown as MediaQueryList;
}

// localStorage is provided by jsdom but guard for odd environments.
if (!window.localStorage) {
  Object.defineProperty(window, "localStorage", {
    value: (() => {
      let store: Record<string, string> = {};
      return {
        getItem: (k: string) => store[k] ?? null,
        setItem: (k: string, v: string) => {
          store[k] = v;
        },
        removeItem: (k: string) => {
          delete store[k];
        },
        clear: () => {
          store = {};
        },
      };
    })(),
  });
}

// Silence Next.js router console noise during tests.
afterEach(() => {
  window.localStorage.clear();
});