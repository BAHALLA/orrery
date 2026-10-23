import "@testing-library/jest-dom/vitest";
import { afterEach, vi } from "vitest";
import { cleanup } from "@testing-library/react";
import { DEFAULT_SPECIALISTS, setActiveSpecialists } from "../specialists";

// jsdom does not implement scrollIntoView; the transcript auto-scroll calls it.
Element.prototype.scrollIntoView = vi.fn();

afterEach(() => {
  cleanup();
  localStorage.clear();
  // A test's /me can name its own specialists; don't let it leak into the next.
  setActiveSpecialists(DEFAULT_SPECIALISTS);
});
