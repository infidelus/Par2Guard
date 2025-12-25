# Changelog

All notable changes to Par2Guard are documented in this file.

## [1.1.2]

### Fixed
- Verification and repair now work correctly for PAR2 sets whose filenames
  begin with special characters (e.g. Ed Sheeran's '`- (Deluxe Edition)`).
- Prevented `par2cmdline` from misinterpreting PAR2 filenames as command-line
  options during verify/repair.

### Notes
- This fixes real-world album naming edge cases such as early Ed Sheeran releases.
- PAR2 creation behaviour is unchanged.

---

## [1.1.1]

### Improved
- Create-mode log output now uses the actual PAR2 archive base name,
  including derived names such as `Album Name - disc 1`.
- Improves clarity when creating multi-disc albums.

---

## [1.1.0]

### Added
- Structured section headers and consistent docstrings throughout the codebase.
- Folder / set name shown in log output before each PAR2 operation.
- Spinner and clearer status feedback during long-running operations.
- Recursive folder inclusion option when adding folders in Create mode.

### Fixed
- Overwrite handling when creating PAR2 files across multiple folders.
- Block size normalization edge cases.
- Log spacing and readability issues.

### Changed
- Codebase reformatted to PEP8 standards for readability and maintainability.

---

## [1.0.1]

### Fixed
- Handling of non-UTF-8 filenames during verify and repair operations.

---

## [1.0.0]

### Initial release
- GUI wrapper for `par2cmdline` supporting create, verify, and repair.
- Clean default logging with optional verbose mode.
- Folder-based PAR2 set handling.

