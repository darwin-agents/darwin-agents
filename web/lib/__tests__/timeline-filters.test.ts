import { describe, expect, it } from "vitest";
import { parseFilters } from "@/components/timeline/filter-bar";

describe("parseFilters", () => {
  it("returns empty strings when no params are present", () => {
    expect(parseFilters(new URLSearchParams())).toEqual({
      kind: "",
      neuron: "",
      tier: "",
    });
  });

  it("reads kind / neuron / tier from URL", () => {
    const sp = new URLSearchParams("kind=event:payment&neuron=content.publisher&tier=reasoning");
    expect(parseFilters(sp)).toEqual({
      kind: "event:payment",
      neuron: "content.publisher",
      tier: "reasoning",
    });
  });

  it("ignores unrelated params and keeps single-value semantics", () => {
    const sp = new URLSearchParams("kind=event:llm&kind=event:payment&unrelated=42");
    // URLSearchParams.get() returns the first value; we don't merge.
    expect(parseFilters(sp).kind).toBe("event:llm");
  });
});
