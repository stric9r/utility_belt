# utility_belt

A collection of helper scripts for STM32 embedded development workflows.

---
## ai/CLAUDE.md

General CLAUDE.md that specifies coding standard and other items that
I personally prefer, but would be useful in other projects.
It's a WIP. To use it, run this command to create a symlink.  Doing it 
this way will ensure you get updates without having to re-copy.

`ln -s [project_root]/ai ~/.claude/CLAUDE.md`

## scripts/stm32cube_to_vscode_sync.py

This was heavily AI generated.  Use for a quick sync between the STM32CubeIDE
project file and .vscode json.

Keeps VS Code IntelliSense in sync with STM32CubeIDE without any manual
maintenance.  It reads the Eclipse CDT `.cproject` XML file that CubeIDE
manages and writes a fresh `.vscode/c_cpp_properties.json` containing the
correct include paths, preprocessor defines, and compiler flags.

**Requirements:** Python 3.7 or later.  No third-party packages — only the
standard library is used.

### The problem it solves

STM32CubeIDE stores all build configuration in `.cproject` (an Eclipse CDT
XML file).  VS Code IntelliSense reads from `.vscode/c_cpp_properties.json`.
These two files are completely independent, so every time CubeIDE regenerates
code or you add a new middleware library, IntelliSense falls behind and starts
showing false red squiggles for valid headers and defines.  This script
bridges that gap by converting one format to the other on demand.

### Usage

```bash
# Normal run — syncs .cproject into c_cpp_properties.json
python3 stm32cube_to_vscode_sync.py --project-root /path/to/your/stm32/project

# If you are already in the project root, use . as the path
python3 stm32cube_to_vscode_sync.py --project-root .

# Relative paths work too
python3 stm32cube_to_vscode_sync.py --project-root ../my-stm32-project
```

`--project-root` is required.  It must point to the directory that contains
`.cproject` and the `.vscode/` folder — the project root as CubeIDE knows it.

### Options

| Flag | Description |
|---|---|
| `--project-root PATH` | **(Required)** Path to the CubeIDE project root. Accepts absolute or relative paths. |
| `--dry-run` | Parse `.cproject` and print the generated JSON to stdout without writing or backing up any files. Use this to preview the output before committing to an update, or to pipe the result into another tool. |
| `--verbose` / `-v` | Enable DEBUG-level logging. Prints every `<option>` element matched during XML parsing, including the count of include paths and defines found in each tool section. Useful when a path or define is missing from the output and you need to trace where it should have come from in `.cproject`. |
| `--h` / `--help` | Show the help message and exit. |

### What it reads from .cproject

The script walks every `<cconfiguration>` block (one per build configuration,
typically `Debug` and `Release`) and extracts:

- **Include paths** — all `<option valueType="includePath">` entries across
  every tool (Assembler, C Compiler, etc.).  The Assembler and C Compiler
  tool sections sometimes carry different subsets, so the script unions them
  to guarantee IntelliSense sees the full list.
- **Preprocessor defines** — all `<option valueType="definedSymbols">` entries,
  also unioned across tools.  Values with assignments (e.g. `MB_TIMER_DEBUG_RED=1`)
  are kept verbatim.
- **MCU part number** — read from the toolchain `option.target_mcu` option
  (e.g. `STM32L552ZETxQ`) and mapped to a GCC `-mcpu=` flag.
- **FPU model** — from the `option.fpu` enumerated option
  (e.g. `fpv5-sp-d16` → `-mfpu=fpv5-sp-d16`).
- **Float ABI** — from the `option.floatabi` enumerated option
  (e.g. `hard` → `-mfloat-abi=hard`).

Eclipse stores include paths relative to the build output directory
(`Debug/` or `Release/`) using a `../` prefix.  The script strips that prefix
and replaces it with `${workspaceFolder}/` so paths are correct in VS Code.
Duplicates across tool sections are removed while preserving the original
ordering.

### What it writes to c_cpp_properties.json

One configuration entry is written per Eclipse build configuration, so the
generated file normally contains both a `Debug` and a `Release` entry.  You
can switch between them in the VS Code status bar while a C file is open
(click the active configuration name in the bottom-right corner).

Fields written from `.cproject` on every run:

| JSON field | Source |
|---|---|
| `name` | Eclipse configuration name (`Debug` / `Release`) |
| `includePath` | Union of all includePath entries, converted to `${workspaceFolder}/...` |
| `defines` | Union of all definedSymbols entries |
| `compilerArgs` | Derived from MCU, FPU, and float-ABI options |

Fields preserved from the existing `c_cpp_properties.json` (not overwritten):

| JSON field | Fallback if file is absent |
|---|---|
| `compilerPath` | `arm-none-eabi-gcc` (searched on `PATH`) |
| `cStandard` | `c11` |
| `cppStandard` | `c++14` |
| `intelliSenseMode` | `gcc-arm` |

These fields are preserved because they are typically set once by the
developer (e.g. an absolute path to a specific toolchain install) and should
not be clobbered on every sync.

### Backups

Before overwriting, the script copies the existing `c_cpp_properties.json` to
a timestamped file in the same directory:

```
.vscode/c_cpp_properties.json.BAK-20260502-143012
```

No backup is created if the file does not yet exist.  Use `--dry-run` if you
want to inspect the output before any files are touched.

### Adding support for a new MCU family

The script maps STM32 part-number prefixes to GCC `-mcpu=` targets using a
lookup table near the top of the file:

```python
MCU_PREFIX_TO_CPU: list[tuple[str, str]] = [
    ("STM32H7",  "cortex-m7"),
    ("STM32L5",  "cortex-m33"),
    ("STM32F4",  "cortex-m4"),
    # ...
]
```

To add a new family, append a tuple to this list.  The table is checked
longest-prefix-first, so more specific entries should come before shorter ones
that could otherwise shadow them.  No other code changes are needed.
