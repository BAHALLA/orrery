# AEP-030: Chat Attachments, Processed and Never Stored

| Field | Value |
|-------|-------|
| **Status** | <span class="badge badge--green">completed</span> |
| **Priority** | <span class="badge badge--blue">P2</span> |
| **Effort** | Medium (2-3 days) |
| **Impact** | Medium-High |
| **Dependencies** | AEP-013 (content defenses), AEP-019 (web console) |

## Gap Analysis

An operator's fastest evidence is often a file: a Grafana screenshot, a log
excerpt, a manifest. The only way to give one to Orrery was to paste text. That
loses screenshots entirely. It also puts long logs into the message box, where
nothing truncates them, redacts their credentials or screens them for
injection the way tool output is screened.

ADK ships `SaveFilesAsArtifactsPlugin`, but it does not fit:

- it trusts the client's declared MIME type;
- it uses the client's file name as the artifact key (a `user:` prefix makes
  the file persist across sessions; on `FileArtifactService` the name becomes
  a path);
- uploaded text skips every content defense;
- the file is stored, which is a retention decision this first version is
  deliberately not making.

## Proposed Solution

Accept images and UTF-8 text files on `POST /chat` and `POST /chat/stream`.
Process each file in memory, show it to the model for the turn it was sent,
and drop it.

### Never stored

The session event records a placeholder line per file, not the file:

```
[attachment 0a1b2c3d: dash.png · image/png · 412 KB — content shown to the model only in the turn it was sent]
```

`AgentGateway.run_in_session` holds the prepared files in a ContextVar for the
turn (`attachment_scope`). `AttachmentPlugin.before_model_callback` adds each
file's content right after its placeholder in every model request of that
turn. Nothing writes the bytes anywhere else: the session store, the memory
service, the audit log and the compaction summary all see the placeholder.
Later turns are not re-billed for the file. The gateway registers the plugin
itself, so no surface can forget it.

Ids are random per upload, so a placeholder typed by a user or replayed from an
older turn matches nothing. A specialist behind an `AgentTool` inherits the
scope: if the coordinator quotes the placeholder, the specialist sees the file
too, in the same turn for the same user.

### Never trusted

| Check | Why |
|---|---|
| Type from the bytes (magic), not `Content-Type` or the extension | The client controls both labels. |
| `Image.open(formats=[...])`: the decoder the magic named, of four | No path to Pillow's TIFF/PSD/… parsers. |
| Pixel budget read from the header, **before** decoding (25 MP) | Decompression bombs. |
| Re-encode every image (JPEG q90, PNG when transparent) | Drops EXIF/GPS and polyglot payloads. |
| Text: strict UTF-8, no NUL, few control characters | Binary refused; PDF/ZIP/executables named in the refusal. |
| Text: credential redaction (`security/redaction.py`) | Same patterns as tool results. |
| Text: instruction-like spans neutralized (`neutralize_text`) | An uploaded log is third-party text, like a tool result. |
| Text wrapped as "data to analyse, not instructions" | The model-side layer of the same defense. |
| File name reduced to `[\w.\- ()]`, 80 chars | The model reads it; it can't forge a placeholder or carry instructions. |
| Caps while streaming: body, per file, file count, fields | An oversized upload is refused at the byte that crosses the limit. |

The multipart body is parsed by `serving/uploads.py` on top of
`python-multipart`'s streaming parser. Starlette's form parser would spool
parts over 1 MB to a temporary file on disk. The rate limit and authentication
run before a byte of the body is read.

### Performance

- Images are downscaled to a 1568 px long edge. That's where Claude stops
  gaining detail, and Gemini and GPT tile similarly. A 4K screenshot goes from
  several MB to roughly 150-300 KB of JPEG. That keeps requests well under
  provider limits and cuts image tokens by about 4×.
- Large JPEGs decode at reduced scale (`draft`); everything else is first
  shrunk with a cheap integer `reduce()` before the LANCZOS pass.
- Decoding runs in worker threads under a process-wide semaphore (2 by
  default), so a burst of uploads queues instead of multiplying peak memory or
  holding the event loop.
- Text is truncated head + tail (200k characters by default: a quarter head,
  the rest tail, since a log's end is usually where the failure is) **before**
  the regex passes, which bounds their cost.
- A turn's files are prepared concurrently.

### Configuration

| Variable | Default | |
|---|---|---|
| `ORRERY_ATTACHMENTS` | `true` | Master switch. `GET /me` reports `attachments: null` when off, and the console hides the paperclip. |
| `ORRERY_ATTACHMENT_IMAGES` | `true` | Set `false` for a model without vision. |
| `ORRERY_ATTACHMENT_MAX_FILES` | `4` | Per message. |
| `ORRERY_ATTACHMENT_MAX_BYTES` | `10485760` | Per file, as uploaded. |
| `ORRERY_ATTACHMENT_MAX_TOTAL_BYTES` | `25165824` | Whole request body. |
| `ORRERY_ATTACHMENT_MAX_PIXELS` | `25000000` | Source image budget. |
| `ORRERY_ATTACHMENT_IMAGE_MAX_EDGE` | `1568` | Long edge sent to the model. |
| `ORRERY_ATTACHMENT_MAX_TEXT_CHARS` | `200000` | Text the model sees per file. |
| `ORRERY_ATTACHMENT_MAX_CONCURRENT_DECODES` | `2` | Image decodes at once, per process. |

A reverse proxy in front must allow the body size (nginx `client_max_body_size`).

### Observability

`orrery_attachments_total{kind, outcome}` counts accepted files and refusals,
with a bounded reason: `too_large`, `unsupported`, `invalid` or `too_many`.
`orrery_attachment_bytes_total{stage}` measures bytes uploaded against bytes
sent to the model. `orrery_attachment_prep_seconds{kind}` times preparation.
Each accepted file is logged with its name, type, size, SHA-256 and processing
note, never its content.

## Not in this version

- **Slack and Google Chat.** Both need an authenticated download from the
  platform (`files:read`; the Chat media API). The gateway already accepts
  `attachments`, so each is an adapter change.
- **PDF, audio, video.** Only Gemini reads audio and video. PDFs would go to
  the model natively, with no server-side parsing; that needs a per-provider
  capability check.
- **Retention.** Files are deliberately not kept, so a later turn can't re-read
  one. If that becomes necessary, the placeholder ids are where an artifact
  store would attach.

The model provider still receives the file, and its own retention policy
applies.
