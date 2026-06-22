# "The Email" layout engine

Renders a cold email as a forensic, full-bleed Gmail reading pane and outputs a
print-ready A2 PNG. The format is the brand thesis made physical: nobody responds
to your cold outbound, so here is the email, enlarged until it is impossible to
ignore and stood on the buyer's desk. The artefact is recognisable inbox chrome
plus the sender's actual words, so it is true by construction and entirely
typographic.

The third production format alongside the newspaper and crossword engines, and a
direct sibling of both: same Pango/Cairo text layout, same resolution-independent
geometry (every position is a fraction of the canvas, so one layout works at any
DPI), same fontconfig font registration, sRGB ICC tagging, the same `--check`
gate and `*.FAILED.png` quarantine, and the same immutable-copy rule.

Unlike the others there is **no template image**: the Gmail chrome (toolbar,
subject, sender row, body, Reply/Reply all/Forward controls) is drawn
procedurally, so the engine is self contained. There is **no AI image
generation** anywhere in the chain; the body is the sender's copy and the engine
just typesets it perfectly at print size.

## Usage

```bash
# Render the worked Qflow example to a preview PNG
python email.py --data test_qflow.json --output qflow_email.png

# Validate at print resolution without writing an image (gates a build)
python email.py --data test_qflow.json --check --print-dpi 300

# Render a print master: exact A2 at 300 DPI, sRGB tagged
python email.py --data test_qflow.json --output print.png --print-dpi 300
```

`--print-dpi` renders straight to an exact A2 (420 x 594 mm) at that DPI; text is
drawn as real glyphs at the output resolution, so sharpness comes from the render
size. `--print-size WIDTHxHEIGHT` overrides the trim, and `--render-size` sets a
preview size (default 1488x2105, roughly 90 DPI). Every saved file carries an
sRGB ICC profile.

## Setup

Same stack as the newspaper engine (Pango/Cairo for text, Pillow for the sRGB
tag). See `../newspaper/setup.sh` for the system libraries, then:

```bash
pip install -r requirements.txt
```

Roboto (Gmail's actual UI face, for forensic fidelity) is bundled in `./fonts`
and registered with fontconfig at runtime, with the shared `../fonts` as a
fallback, so no system font install is needed. Override the font directory with
`SENTRADA_EMAIL_FONT_DIR`.

## JSON schema (the Copy Agent / P4 contract)

`test_qflow.json` is a complete worked example.

```json
{
  "copy_source": "authored",
  "account": { "unread_count": 1284 },
  "sender": {
    "name": "Joe",
    "email": "joe@sentrada.io",
    "avatar_initial": "J",
    "avatar_color": "#C05933"
  },
  "recipient": { "name": "James" },
  "subject": "We printed this so you’d actually read it",
  "timestamp": "09:14",
  "label": "Inbox",
  "body": [
    { "type": "p", "text": "Hi James," },
    { "type": "bullet", "text": "An observed, company-specific detail." },
    { "type": "signature", "lines": ["Joe", "Sentrada"] }
  ]
}
```

| Field | Notes |
|-------|-------|
| `copy_source` | `authored` (we or the chain wrote it) or `client` (client supplied, rendered verbatim). Provenance only; the render is identical. |
| `account.unread_count` | The "1 of N" the recipient sees: their drowning inbox. Optional. |
| `sender.name` / `sender.email` | Whoever is sending. They sign the piece. Required. |
| `sender.avatar_initial` | Optional; derived from the name if absent. |
| `sender.avatar_color` | Optional hex for the avatar disc (defaults to Sentrada clay). |
| `recipient.name` | Used only to set the "to" line; defaults to the forensic "to me". Override with `recipient.to_label`. |
| `subject` | The hook. Required. |
| `timestamp` | The time shown in the header, e.g. "09:14". Optional. |
| `body[]` | Ordered blocks: `p` (paragraph), `bullet`, or `signature` (`lines: [...]`). The signature is best last. |

Punctuation is normalised to typographic forms at render time (curly quotes,
ellipsis). The engine never manufactures an em dash (house rule). House copy
rules (British English, no em dashes, no exclamation marks) apply to our authored
copy; client-supplied copy is rendered verbatim.

**Copy is immutable.** The engine never truncates or squeezes text to fit. If the
body overflows the sheet at the chosen size, that is a hard `--check` failure for
P4 (or the client) to shorten, never an edit. On a normal render an overflow
withholds the deliverable and writes a `*.FAILED.png` for inspection.

## Two copy sources

The same schema serves both routes, so the engine does not care where the words
came from:

- **Authored.** The Copy agent writes the email from the research brief, or the
  founder writes it by hand. House copy rules apply.
- **Client-supplied.** The client provides their own email; it is rendered
  verbatim, with only punctuation normalised and the fit check run. Via the
  runner: `sentrada_runner.py generate --format email --email-copy their_email.json`.

## What the engine draws

- **Toolbar** (back, archive, report spam, delete, mark unread, snooze, more) with
  the "1 of N" inbox position and prev chevron on the right.
- **Subject** in Roboto Medium with the folder label chip ("Inbox").
- **Sender row**: a coloured avatar disc with the sender's initial, the sender name
  in bold with `<email>` beside it, the "to me" line, the timestamp, and the star /
  reply / controls.
- **Body**: the cold email itself, paragraphs and bullets in Roboto, with the
  sign-off in bold.
- **Action pills**: Reply, Reply all, Forward.

## Pre-render check

```bash
python email.py --data company.json --check --print-dpi 300
```

Prints PASS/FAIL at print resolution and exits non-zero on any failure (currently:
the body overflowing the sheet), so it can gate a build. On failure during a normal
render the image is written to a `*.FAILED.png` path and the deliverable is withheld.
```
