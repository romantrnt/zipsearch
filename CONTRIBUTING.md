# Contributing

Use Python 3.10 or newer. Keep the package dependency-free unless a dependency provides a clear correctness or safety benefit.

```console
python -m pip install -e . pytest ruff
pytest
ruff check .
```

Please add generated test fixtures rather than committing real archives or data. Search code must preserve bounded memory, avoid `extractall`, and report a bad archive/member without aborting unrelated work.
