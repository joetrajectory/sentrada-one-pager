# Bundled fonts

These variable fonts are used by the newspaper layout engine and registered with
fontconfig at runtime (see `../newspaper/newspaper.py`), so no system install is
needed.

| File | Family | Used for |
| --- | --- | --- |
| PlayfairDisplay.ttf | Playfair Display | masthead, headlines, stat number |
| PlayfairDisplay-Italic.ttf | Playfair Display Italic | pull quote |
| Lora.ttf | Lora | body copy, sidebar body |
| Lora-Italic.ttf | Lora Italic | reserved |
| Inter.ttf | Inter | bylines, edition line, stat source |

All three families are from Google Fonts and licensed under the SIL Open Font
License 1.1, which permits bundling and redistribution:

- Playfair Display: https://fonts.google.com/specimen/Playfair+Display
- Lora: https://fonts.google.com/specimen/Lora
- Inter: https://fonts.google.com/specimen/Inter
