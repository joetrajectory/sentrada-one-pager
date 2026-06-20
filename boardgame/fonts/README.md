# Bundled fonts (board game engine)

Registered with fontconfig at runtime by `../boardgame.py`, so no system install
is needed. The shared `../../fonts` directory is also registered as a fallback.

| File | Family | Used for |
| --- | --- | --- |
| Fraunces-Bold.ttf | Fraunces (Bold) | title: THE / [COMPANY] / GAME |
| Fraunces-Italic.ttf | Fraunces (Italic) | subtitle strapline |
| Inter.ttf | Inter | segment copy, START / FINISH, numbers, credit |

Fraunces is the premium display serif called for by the format spec. The bundled
faces are static instances cut from the Google Fonts variable font (Bold at the
display optical size; Italic at a text optical size), so Pango selects them
reliably without depending on variable-font axis support. Inter matches the
sibling engines.

Both families are from Google Fonts under the SIL Open Font License 1.1, which
permits bundling and redistribution:

- Fraunces: https://fonts.google.com/specimen/Fraunces
- Inter: https://fonts.google.com/specimen/Inter
