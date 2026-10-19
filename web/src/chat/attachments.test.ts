import { describe, expect, it } from "vitest";
import type { AttachmentsInfo } from "../api/types";
import { checkFiles, splitAttachments } from "./attachments";

const info: AttachmentsInfo = {
  max_files: 2,
  max_file_bytes: 1024,
  max_total_bytes: 1500,
  images: true,
  image_types: ["image/png", "image/jpeg", "image/gif", "image/webp"],
};

const file = (name: string, size: number, type = "") =>
  new File(["x".repeat(size)], name, { type });

describe("splitAttachments", () => {
  it("turns the server's placeholder lines back into chips", () => {
    const text =
      "why is it red?\n" +
      "[attachment 0a1b2c3d: dash.png · image/png · 412 KB — content shown to the model only in the turn it was sent]\n" +
      "[attachment deadbeef: app.log · text/plain · 9 B — content shown to the model only in the turn it was sent]";

    expect(splitAttachments(text)).toEqual({
      text: "why is it red?",
      attachments: [
        { name: "dash.png", type: "image/png", size: "412 KB" },
        { name: "app.log", type: "text/plain", size: "9 B" },
      ],
    });
  });

  it("leaves ordinary text alone, brackets included", () => {
    const text = "[attachment] is not a placeholder";
    expect(splitAttachments(text)).toEqual({ text, attachments: [] });
  });
});

describe("checkFiles", () => {
  it("accepts images and text within the limits", () => {
    const { files, errors } = checkFiles(
      [],
      [file("a.png", 10, "image/png"), file("b.log", 10)],
      info,
    );
    expect(files.map((f) => f.name)).toEqual(["a.png", "b.log"]);
    expect(errors).toEqual([]);
  });

  it("explains each refusal", () => {
    const { files, errors } = checkFiles(
      [],
      [
        file("big.log", 2048),
        file("doc.pdf", 10, "application/pdf"),
        file("pic.bmp", 10, "image/bmp"),
        file("ok.txt", 10),
      ],
      info,
    );
    expect(files.map((f) => f.name)).toEqual(["ok.txt"]);
    expect(errors).toEqual([
      "big.log is over the 1 KB limit.",
      "doc.pdf: only images and text files are supported.",
      "pic.bmp: use PNG, JPEG, GIF or WebP.",
    ]);
  });

  it("enforces the count and total size, and skips a duplicate", () => {
    const a = file("a.log", 1000);
    expect(checkFiles([a], [file("b.log", 1000)], info).errors).toEqual([
      "b.log would take the message over 1.5 KB.",
    ]);
    expect(checkFiles([a], [a], info)).toEqual({ files: [a], errors: [] });
    expect(checkFiles([a, file("b", 1)], [file("c", 1)], info).errors).toEqual([
      "At most 2 files per message.",
    ]);
  });

  it("refuses images when the deployment's model can't read them", () => {
    const { errors } = checkFiles([], [file("a.png", 10, "image/png")], {
      ...info,
      images: false,
      image_types: [],
    });
    expect(errors).toEqual(["a.png: this deployment doesn't accept images."]);
  });
});
