import { describe, expect, it } from "vitest";
import { urlBase64ToUint8Array } from "@/lib/push";

describe("urlBase64ToUint8Array", () => {
  it("decodes a base64url string with no padding", () => {
    // 'foo' → 'Zm9v' (no padding needed).
    expect(urlBase64ToUint8Array("Zm9v")).toEqual(
      new Uint8Array([0x66, 0x6f, 0x6f]),
    );
    // 'fooba' → 'Zm9vYmE' (one '=' of padding once normalized).
    expect(urlBase64ToUint8Array("Zm9vYmE")).toEqual(
      new Uint8Array([0x66, 0x6f, 0x6f, 0x62, 0x61]),
    );
  });

  it("converts base64url alphabet (- _) to base64 (+ /)", () => {
    // Two bytes 0xff 0xee → base64 '/+4=' → base64url '_-4'.
    const out = urlBase64ToUint8Array("_-4");
    expect(out).toEqual(new Uint8Array([0xff, 0xee]));
  });

  it("matches a known VAPID-style 65-byte P-256 public key length", () => {
    // 65 raw bytes → 88 base64 chars (with padding), 87 unpadded base64url.
    const raw = new Uint8Array(65);
    for (let i = 0; i < raw.length; i++) raw[i] = i;
    const b64 = Buffer.from(raw).toString("base64");
    const b64url = b64.replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
    const decoded = urlBase64ToUint8Array(b64url);
    expect(decoded.length).toBe(65);
    expect(Array.from(decoded)).toEqual(Array.from(raw));
  });
});
