import { describe, expect, it } from "vitest";
import { MAX_OVERLAP_WORDS, dropLeadingOverlap, dropTrailingOverlap, joinChunks, overlapMerge, overlapWords } from "./joinChunks";

describe("overlapWords", () => {
  it("finds the longest run that ends one text and begins the next", () => {
    expect(overlapWords("we shipped the build on friday and then", "friday and then the tests ran")).toBe(3);
    expect(overlapWords("one two three", "four five six")).toBe(0);
  });

  it("ignores case and punctuation", () => {
    expect(overlapWords("It was raining, and the", "And the. Roads were wet")).toBe(2);
  });

  it("takes a word the cut fell in as the same word seen whole in only one chunk", () => {
    // the first chunk heard "jumps" whole; the second only "mps" ... but needs four letters to say so
    expect(overlapWords("the quick brown fox jumps", "umps over the lazy dog")).toBe(1);   // "jumps" ends with "umps"
    expect(overlapWords("the quick brown fox jump", "jumps over the lazy dog")).toBe(1);    // the first chunk heard only the start
    expect(overlapWords("pass the salt and the", "then pepper")).toBe(0);                    // "the" vs "then" is not an echo
  });

  it("does not let a partial match reach inside the run", () => {
    expect(overlapWords("alpha bravo charlie", "brav charlie delta")).toBe(0);
    expect(overlapWords("alpha bravo charlie", "bravo charlie delta")).toBe(2);
  });
});

describe("dropping an overlap", () => {
  it("removes the repeated words from the head of the later text", () => {
    expect(dropLeadingOverlap("i think we should go to the", "go to the market today")).toBe("market today");
  });
  it("removes them from the tail of the earlier text when the later one is already placed", () => {
    expect(dropTrailingOverlap("i think we should go to the", "go to the market today")).toBe("i think we should");
  });
  it("leaves unrelated text whole", () => {
    expect(dropLeadingOverlap("hello there", "something else")).toBe("something else");
  });
});

describe("a word the cut fell in (the previous chunk heard only its start)", () => {
  it("keeps the whole word and drops the fragment", () => {
    expect(joinChunks([
      { text: "we sent it to the transcrip", overlaps: false },
      { text: "transcription service for the rest", overlaps: true },
    ])).toBe("we sent it to the transcription service for the rest");
    expect(overlapMerge("we sent it to the transcrip", "transcription service")).toEqual({ dropBefore: 1, dropAfter: 0 });
  });
  it("in a longer run, drops the fragment and the words the run shares from the earlier text", () => {
    expect(joinChunks([
      { text: "one two three fox jump", overlaps: false },
      { text: "fox jumps over it", overlaps: true },
    ])).toBe("one two three fox jumps over it");
  });
  it("still drops the later text's head when it is the later chunk that heard only the end", () => {
    expect(overlapMerge("the quick brown fox jumps", "umps over the lazy dog")).toEqual({ dropBefore: 0, dropAfter: 1 });
  });
  it("looks for at most four words, so a spoken repetition further in is not an echo", () => {
    expect(MAX_OVERLAP_WORDS).toBe(4);
    expect(overlapWords("a b c d e f g", "d e f g h i")).toBe(4);
    expect(overlapWords("one two three four five six", "two three four five six seven")).toBe(0);
  });
});

describe("joinChunks", () => {
  it("joins in order with the overlap said once", () => {
    const text = joinChunks([
      { text: "first we plan the work and then", overlaps: false },
      { text: "and then we build it carefully", overlaps: true },
      { text: "carefully and test every part", overlaps: true },
    ]);
    expect(text).toBe("first we plan the work and then we build it carefully and test every part");
  });

  it("does not deduplicate across a cut at a pause: the same word either side is speech", () => {
    expect(joinChunks([
      { text: "this is very", overlaps: false },
      { text: "very good", overlaps: false },
    ])).toBe("this is very very good");
  });

  it("skips a chunk that heard nothing without losing the overlap bookkeeping", () => {
    expect(joinChunks([
      { text: "alpha beta gamma", overlaps: false },
      { text: "   ", overlaps: true },
      { text: "delta epsilon", overlaps: true },
    ])).toBe("alpha beta gamma delta epsilon");
  });

  it("is empty when nothing was heard", () => {
    expect(joinChunks([{ text: "", overlaps: false }])).toBe("");
  });
});
