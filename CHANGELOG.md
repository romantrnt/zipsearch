# Changelog

## 6.0.0

ZipSearch 6.0.0 completes the archive-native search architecture.

- Added the optional compact, archive-scoped candidate index with incremental updates, verification, compaction, explainable planning, and safe `INDEX`/`SCAN`/`HYBRID` execution.
- Added deterministic entity extraction and related-occurrence navigation for phones, emails/domains, URLs, IP addresses, UUIDs, and hashes.
- Added advanced Boolean queries, structured provenance, deterministic result grouping, and optional local TUI history/root state.
- Added direct support for DOCX, PPTX, ODT, SQLite, XLSX, and bounded nested ZIP traversal.
- Expanded safety controls, index integrity checks, differential parity tests, terminal-state tests, benchmarks, and stdlib-only zipapp packaging.

The archive remains authoritative: generated state is disposable acceleration and every indexed result is verified against its source archive before output.
