## What this changes

<!-- What it does for someone using ccdash, in a sentence or two. -->

## Checks

- [ ] `python3 ccdash.py --selftest` prints `ok`
- [ ] `ruff check ccdash.py` is clean
- [ ] No new runtime dependency — standard library only, Pillow still optional
- [ ] Still runs on Python 3.8
- [ ] README updated, if a setting, a key or the behaviour behind one changed

## If this touches the paint loop

`render`, `paint`, `sync`, the panels or the art. Paste `ccdash --bench 200` from before and
after:

```
before:
after:
```
