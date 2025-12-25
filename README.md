# Par2Guard

Par2Guard is a modern Python 3 GUI for creating, verifying, and repairing PAR2 parity files.  
It provides a user-friendly desktop interface for the standard `par2cmdline` tool, making parity operations easy and accessible.

---

![Par2Guard main window](screenshots/main_window.png)

---

## 📦 Features

Par2Guard provides:

- Create PAR2 parity files with configurable redundancy
- Verify existing PAR2 sets (single files or whole folder trees)
- Repair damaged or missing files when sufficient parity data exists
- Clean, readable logging with optional verbose output
- Clear end-of-run summaries showing:
  - items that are OK
  - items that require repair
  - items that were repaired or failed
- Automatic creation of one PAR2 set per folder when multiple folders are selected
- Automatically names PAR2 sets for multi-disc albums as “Album – disc N”
- GTK 3 desktop GUI with intuitive controls
- Project-local configuration file (`config.ini`) for persistent settings

---

## 📋 Requirements

- Python 3.9 or newer
- GTK 3 with PyGObject
- `par2cmdline` (`par2`) installed and in your PATH

---

## 🛠 Installing Dependencies (Ubuntu / Linux Mint)

```bash
sudo apt install par2 python3-gi python3-gi-cairo gir1.2-gtk-3.0

```

---

## Installation

Par2Guard does not require installation and can be run directly from its folder.

```bash
git clone https://github.com/infidelus/par2guard.git
cd par2guard
chmod +x par2guard.py
./par2guard.py
```

On first run, a `config.ini` file will be created in the same directory.

---

## Configuration

Par2Guard uses a simple project-local configuration file named `config.ini`.

Example:

```ini
[defaults]
default_path=/home/youruser/Music
redundancy_percent=10
verbose_logging=0
```

- default_path – Starting folder used when file chooser dialogs open
- redundancy_percent – Default redundancy percentage for PAR2 creation
- verbose_logging – 0 for clean output, 1 to show full PAR2 progress

The app also remembers the last opened folder during the current session.

---

## Usage

### Creating PAR2 files

1. Open the Create tab
2. Add files or one or more folders
3. Choose redundancy or recovery block settings
4. Click Create PAR2

If multiple folders are selected, each will get its own PAR2 set.

---

### Verifying files

1. Open the Verify tab
2. Add .par2 files or scan a folder recursively
3. Click Verify

At the end, a summary is displayed for items requiring repair.

---

### Repairing files

1. Open the Repair tab
2. Add .par2 files or scan a folder
3. Click Repair

Only the main .par2 file is required — associated .vol*.par2 files are included automatically.

---

## Desktop Integration (Optional)

You can add Par2Guard to your desktop environment using a .desktop file. For example:

```ini
[Desktop Entry]
Version=1.1.0
Type=Application
Name=Par2Guard
Comment=Create, verify and repair PAR2 parity files
Exec=/full/path/to/par2guard.py
Icon=/full/path/to/par2guard.png
Terminal=false
Categories=Utility;FileTools;
StartupNotify=true
```

Copy the file to:

```bash
~/.local/share/applications/
```

---

## Version history

A detailed list of changes for each release can be found in
[CHANGELOG.md](CHANGELOG.md).

---

## About PAR2

PAR2 files are parity recovery files that allow reconstruction of damaged or missing data, provided sufficient recovery blocks exist. They are commonly used for backups, large media collections, and archives. Par2Guard invokes the system par2 / par2cmdline tool for all operations.

---

## Credits

Par2Guard is inspired by the original PyPAR2 project:

https://pypar2.fingelrest.net/

This project is not affiliated with PyPAR2. It exists to provide a modern Python 3 replacement with similar goals.

---

## License

Par2Guard is released under the MIT License. See the LICENSE file for details.

---

## Status

Par2Guard is considered stable for regular use.
