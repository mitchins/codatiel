# Logo assets

The mark is a cockatiel head built from code glyphs: a `//` crest, a `C`
head, a `>` beak and an orange cheek.

| File | Use |
|---|---|
| `logo.svg` | The mark, 24px and up. |
| `logo-16.svg` | Heavier strokes for 16–20px (favicon, badges). |
| `logo-wordmark.svg` | Mark plus "codatiel", drawn as paths (no font needed). |
| `logo-animated.svg` | Plays once, under 5s; rests as the full logo. |
| `logo-badge.svg` | Mark on its own dark tile, for backgrounds you don't control. |
| `logo.txt`, `logo-large.txt` | ASCII logos. |
| `ascii_anim.py` | Terminal animation; prints the plain logo when not a TTY. |

## Colours

Ink `#77808c` and orange `#e36209`, with no light/dark switching. Both
hold at least 3:1 contrast on white, `#f6f8fa`, `#0d1117`, `#010409` and
`#22272e`. They do not hold on mid-grey; use the badge there.

## Embedding

- `<img>` needs its own `alt`: `alt="Codatiel"`, or `alt=""` beside a
  heading that already says the name.
- Wrap ASCII art as `<pre role="img" aria-label="Codatiel">`.
