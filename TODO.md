# TODO

Roughly in priority order.

## Check on real paper

So far the `label` and `tag` layouts have only been checked as `--preview` images.

- [ ] Test-print each profile with the files in `examples/`.
- [ ] Check darkness 2 for large bold text (bleeding, fading at the end of long jobs) and pick the default for `label` and `tag`.
- [ ] Check that sideways labels come out the right way round, with the start of the name printed first. If not, switch `ROTATE_270` to `ROTATE_90` in `render_job`.
- [ ] Measure a printed label to confirm 8 dots per mm and the 48mm printable width.
- [ ] Check whether the 255-row chunks in `print_rows` leave visible seams in large solid text.

## Printing reliability

- [ ] Show a clear error when the printer is off or out of range, instead of a raw socket traceback, and retry the connection once.
- [ ] Check the battery before long jobs and warn when it's low.
- [ ] Tune `row_delay` for large solid areas, which may need a slower feed to print evenly.

## Tests and tooling

- [ ] Add pytest tests for the input parsers: text blocks, `field: value` blocks, CSV header detection, and each JSON shape.
- [ ] Add tests for profile loading: overrides, unknown keys, wrong value types, and user profiles replacing built-ins.
- [ ] Add golden-image tests that render each profile from `examples/` and compare pixels. `docs/make_images.py` already renders every example and could be the starting point.
- [ ] Add ruff for linting and formatting.

## Packaging and setup

- [ ] Add a `pyproject.toml` with a `ppa6` command and the built-in profiles as package data, splitting `ppa6.py` into modules (driver, layout, input parsing, CLI) along the way. The tool switches to `/usr/bin/python3` to get Bluetooth support, so a pipx or venv install still needs Pillow in the system Python; either document that or find a better approach.
- [ ] Add a config file (`~/.config/ppa6/config.toml`) for the default printer address, profile, and darkness, instead of relying only on `PERIPAGE_MAC`.
- [ ] Add shell completion, including profile names.

## Label features

- [ ] `--copies N` for printing several of the same label.
- [ ] Support die-cut sticker rolls: a fixed label pitch, plus gap detection if the printer reports it.
- [ ] An option to give every label in a batch the same length, for a tidy shelf.
- [ ] Image and QR code fields, such as an icon next to the name or a QR code linking to an inventory list.
- [ ] Per-record overrides in the input, like a `size:` or `profile:` key on one label.
- [ ] A fallback font for emoji and symbols, which DejaVu doesn't cover.
- [ ] A white-on-black title bar style.
- [ ] Support other PeriPage models with different paper widths by making `ROW_WIDTH` a per-printer setting.
