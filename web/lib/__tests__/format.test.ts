import { describe, expect, it } from "vitest";
import { formatUsdc, formatSignedUsdc, shortId } from "../format";

describe("formatUsdc", () => {
  it("formats USD with two decimals", () => {
    expect(formatUsdc(12.4)).toBe("$12.40");
  });
  it("falls back to em-dash for nullish", () => {
    expect(formatUsdc(null)).toBe("—");
    expect(formatUsdc(undefined)).toBe("—");
  });
  it("parses numeric strings", () => {
    expect(formatUsdc("3.5")).toBe("$3.50");
  });
});

describe("formatSignedUsdc", () => {
  it("prefixes a + for positives", () => {
    expect(formatSignedUsdc(1.25)).toBe("+$1.25");
  });
  it("keeps negatives signed by Intl", () => {
    expect(formatSignedUsdc(-1.25)).toMatch(/-\$1\.25/);
  });
});

describe("shortId", () => {
  it("keeps short ids whole", () => {
    expect(shortId("abc")).toBe("abc");
  });
  it("truncates with an ellipsis in the middle", () => {
    expect(shortId("aaaaaaaaaaaa", 4, 4)).toBe("aaaa…aaaa");
  });
});
