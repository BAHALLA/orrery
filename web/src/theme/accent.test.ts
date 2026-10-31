import { describe, expect, it } from "vitest";
import { applyAccent, brandedTitle, resolveBranding } from "./accent";

describe("branding", () => {
  it("defaults to the product name with no environment or accent", () => {
    expect(resolveBranding(undefined)).toEqual({
      title: "Orrery Console",
      environment: null,
      accent: null,
    });
  });

  it("drops values the console cannot show, rather than breaking the page", () => {
    expect(resolveBranding({ title: 42 as never, accent: "constructor" })).toEqual({
      title: "Orrery Console",
      environment: null,
      accent: null,
    });
    expect(resolveBranding({ title: "  ", environment: "prod", accent: "#f00" })).toEqual({
      title: "Orrery Console",
      environment: null,
      accent: null,
    });
  });

  it("puts the environment first in the tab title", () => {
    const b = resolveBranding({ title: "Lab Ops", environment: "production" });
    expect(brandedTitle(b)).toBe("PRODUCTION · Lab Ops");
    expect(brandedTitle(resolveBranding({ title: "Lab Ops" }))).toBe("Lab Ops");
    expect(brandedTitle(b, "(2) Approval needed")).toBe(
      "PRODUCTION · (2) Approval needed · Lab Ops",
    );
  });

  it("repaints the brand scale in the accent's hue", () => {
    const el = document.createElement("div");
    applyAccent("red", el);
    expect(el.style.getPropertyValue("--color-brand-600")).toBe("oklch(0.51 0.21 25)");
    expect(el.style.getPropertyValue("--color-brand-50")).toBe("oklch(0.97 0.02 25)");
  });

  it("leaves the stylesheet alone for the default accent", () => {
    const el = document.createElement("div");
    applyAccent("indigo", el);
    applyAccent(null, el);
    expect(el.style.length).toBe(0);
  });
});
