#!/usr/bin/env python3
"""
stm32cube_to_vscode_sync.py — STM32CubeIDE to VS Code IntelliSense sync tool.

Reads the Eclipse CDT .cproject XML produced by STM32CubeIDE and writes a
fresh .vscode/c_cpp_properties.json so that VS Code's C/C++ IntelliSense
extension sees the same include paths, preprocessor defines, and compiler
flags that the IDE uses to build the firmware.

The existing c_cpp_properties.json is backed up with a timestamp suffix
before any changes are made.

Usage:
    python3 Middlewares/stm32cube_to_vscode_sync.py --project-root /path/to/project
    python3 Middlewares/stm32cube_to_vscode_sync.py --project-root ../my-stm32 --dry-run
    python3 Middlewares/stm32cube_to_vscode_sync.py --project-root . --verbose

--project-root is required and can be any absolute or relative path to the
directory that contains .cproject and the .vscode/ folder.
"""


# Standard-library imports only — no pip dependencies required!
from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional


# Filenames — relative to the project root unless noted.
CPROJECT_FILENAME    = ".cproject"
VSCODE_DIR           = ".vscode"
CPP_PROPS_FILENAME   = "c_cpp_properties.json"

# The integer schema version the VS Code C/C++ extension expects.
# Increment this if Microsoft changes the format and the extension complains.
CPP_PROPS_VERSION = 4

# Token VS Code substitutes at runtime with the workspace (project) root path.
# Using this token instead of an absolute path keeps the generated file
# portable across developer machines.
WORKSPACE_FOLDER = "${workspaceFolder}"

# Fallback IntelliSense settings
#
# These are used when the existing c_cpp_properties.json is absent or missing
# a key.
FALLBACK_COMPILER_PATH = "arm-none-eabi-gcc"
FALLBACK_C_STANDARD    = "c11"
FALLBACK_CPP_STANDARD  = "c++14"
FALLBACK_INTELLISENSE  = "gcc-arm"

# Compiler flags that apply to every ARM Cortex-M configuration.
#
# -mthumb is universal: all Cortex-M cores execute in Thumb / Thumb-2 state.
# FPU and float-ABI flags are derived per-configuration from .cproject data
# and appended in build_compiler_args().
BASE_COMPILER_ARGS: list[str] = [
    "-mthumb",
]

# MCU part-number prefix to GCC -mcpu= target name lookup table.
#
# Eclipse stores the full part string (ex. "STM32L552ZETxQ") in .cproject.
# We strip the suffix and match the family prefix to determine the CPU target.
#
# The list is ordered from most-specific to least-specific so that a longer
# prefix (ex. "STM32WB") is matched before a hypothetical shorter one would
# shadow it.  Add new families here; no other code changes are needed.
MCU_PREFIX_TO_CPU: list[tuple[str, str]] = [
    # Cortex-M7
    ("STM32H7",  "cortex-m7"),
    ("STM32F7",  "cortex-m7"),
    # Cortex-M33 (TrustZone, ARMv8-M)
    ("STM32L5",  "cortex-m33"),
    ("STM32U5",  "cortex-m33"),
    # Cortex-M4 (with or without FPU)
    ("STM32F4",  "cortex-m4"),
    ("STM32F3",  "cortex-m4"),
    ("STM32G4",  "cortex-m4"),
    ("STM32L4",  "cortex-m4"),
    ("STM32WB",  "cortex-m4"),   # dual-core WB: M4 is the application core
    # Cortex-M3
    ("STM32F2",  "cortex-m3"),
    ("STM32F1",  "cortex-m3"),
    ("STM32L1",  "cortex-m3"),
    # Cortex-M0 / M0+
    ("STM32F0",  "cortex-m0plus"),
    ("STM32G0",  "cortex-m0plus"),
    ("STM32L0",  "cortex-m0plus"),
    ("STM32WL",  "cortex-m0plus"),  # WL dual-core: M0+ is the radio sub-GHz core
]


# Data structures
#
# Using a dataclass keeps parsed data self-documenting and separates the
# "extraction" phase (reading XML) from the "translation" phase (writing JSON).
# Adding a new field here is the only change needed to thread new data through
# the pipeline.
@dataclass
class EclipseConfig:
    """
    All IntelliSense-relevant settings extracted from one Eclipse build
    configuration (a <cconfiguration> block in .cproject).

    A single .cproject typically contains "Debug" and "Release" configurations;
    each produces one EclipseConfig instance and, ultimately, one VS Code
    configuration entry in c_cpp_properties.json.
    """

    # Human-readable name from the Eclipse storageModule, ex. "Debug".
    name: str

    # Include paths already converted to VS Code form:
    # "${workspaceFolder}/Drivers/CMSIS/Include", etc.
    # Insertion order is preserved; duplicates are removed at parse time.
    include_paths: list[str] = field(default_factory=list)

    # Preprocessor symbols, ex. "USE_HAL_DRIVER", "STM32L552xx", "DEBUG".
    # Values with assignments are kept verbatim, ex. "MB_TIMER_DEBUG_RED=1".
    defines: list[str] = field(default_factory=list)

    # Raw MCU part number string from .cproject, ex. "STM32L552ZETxQ".
    # Used by mcu_to_cpu() to derive the -mcpu= compiler flag.
    mcu: Optional[str] = None

    # FPU model extracted from the Eclipse enumerated option value,
    # ex. "fpv5-sp-d16".  None means the configuration uses software float.
    fpu: Optional[str] = None

    # Floating-point ABI: "hard", "softfp", or "soft".
    # None means the GCC default (soft) applies.
    float_abi: Optional[str] = None


# XML parsing helpers
#
# Eclipse CDT uses a dot-separated Java-style naming convention for the
# 'superClass' attribute of <option> elements, ex.:
#   com.st.stm32cube.ide.mcu.gnu.managedbuild.option.fpu
#
# We match options by checking whether their superClass *contains* a known
# token rather than matching the full string.  This keeps the code robust
# against STM32CubeIDE version bumps that may add or change numeric suffixes.

# Substrings of 'superClass' that identify each option type we care about.
_SC_FPU      = "option.fpu"
_SC_FLOATABI = "option.floatabi"
_SC_MCU      = "option.target_mcu"

# CDT encodes the selected value of an enumerated option as a fully-qualified
# ID with ".value." separating the option name from the chosen value, ex.:
#   com.st.stm32cube.ide.mcu.gnu.managedbuild.option.floatabi.value.hard
# Everything after the last ".value." is the usable token ("hard").
_VALUE_INFIX = ".value."


def _extract_enum_value(raw: str) -> str:
    """
    Pull the human-readable token out of a CDT enumerated attribute value.

    Example:
        "…option.floatabi.value.hard"  →  "hard"
        "…option.fpu.value.fpv5-sp-d16"  →  "fpv5-sp-d16"

    If ".value." is absent the raw string is returned unchanged so that plain
    string options (like the MCU part number) pass through unmodified.
    """
    idx = raw.rfind(_VALUE_INFIX)
    return raw[idx + len(_VALUE_INFIX):] if idx != -1 else raw


def _superclass_contains(option_elem: ET.Element, token: str) -> bool:
    """
    Return True if the <option> element's 'superClass' attribute contains
    *token* as a substring.

    CDT superClass values function like Java fully-qualified class names.
    Substring matching is more resilient than an exact match because individual
    <option> element IDs carry version-specific numeric noise, but superClass
    values remain stable across IDE versions.
    """
    return token in option_elem.get("superClass", "")


def _collect_list_values(option_elem: ET.Element) -> list[str]:
    """
    Return the 'value' of every <listOptionValue> child inside *option_elem*.

    CDT serializes multi-value settings (include paths, defined symbols) as
    a parent <option> with one <listOptionValue value="..."> per entry.
    Blank values are silently skipped.
    """
    return [
        child.get("value", "")
        for child in option_elem.findall("listOptionValue")
        if child.get("value", "")  # skip accidentally empty entries
    ]


def _convert_path(raw: str) -> str:
    """
    Translate an Eclipse CDT include path to a VS Code workspace-relative form.

    WHY the translation is needed:
      Eclipse stores paths relative to the *build output directory* (Debug/ or
      Release/ inside the project root), so every user-added include starts
      with "../" to climb back up to the project root.  VS Code IntelliSense
      expects paths anchored at ${workspaceFolder} (the project root), not at
      a build sub-directory.

    Conversion rule:
      "../Inc"  →  "${workspaceFolder}/Inc"

    Special cases passed through unchanged:
      • Absolute paths (start with "/") — ex. system toolchain headers.
      • Eclipse variable paths (start with "${") — ex. ${gnu_tools_...}.
        IntelliSense cannot resolve Eclipse variables anyway; the user would
        need to expand them manually in c_cpp_properties.json.
    """
    if raw.startswith("/") or raw.startswith("${"):
        return raw

    # Strip the leading "../" that CDT adds because paths are relative to the
    # build sub-directory rather than to the project root.
    if raw.startswith("../"):
        raw = raw[3:]

    return f"{WORKSPACE_FOLDER}/{raw}"


def _ordered_unique(items: list[str]) -> list[str]:
    """
    Remove duplicates from *items* while preserving insertion order.

    dict.fromkeys() preserves insertion order (guaranteed since Python 3.7)
    while discarding subsequent occurrences of each key.  This is preferable
    to a set because the original ordering in .cproject may reflect intentional
    include-search priorities.
    """
    return list(dict.fromkeys(items))


# .cproject parser

def parse_cproject(cproject_path: Path) -> list[EclipseConfig]:
    """
    Parse *cproject_path* and return one EclipseConfig per build configuration.

    Eclipse CDT .cproject relevant structure (abbreviated):

        <cproject>
          <storageModule moduleId="org.eclipse.cdt.core.settings">
            <cconfiguration id="…debug…">
              <storageModule moduleId="org.eclipse.cdt.core.settings" name="Debug"/>
              <storageModule moduleId="cdtBuildSystem">
                <configuration name="Debug">
                  <folderInfo>
                    <toolChain>
                      <!-- scalar options live directly on toolChain -->
                      <option superClass="…option.target_mcu" value="STM32L552ZETxQ"/>
                      <option superClass="…option.fpu"        value="…value.fpv5-sp-d16"/>
                      <option superClass="…option.floatabi"   value="…value.hard"/>

                      <!-- list options live inside <tool> elements -->
                      <tool name="MCU/MPU GCC Assembler">
                        <option valueType="includePath">
                          <listOptionValue value="../Inc"/>
                          …
                        </option>
                        <option valueType="definedSymbols">
                          <listOptionValue value="DEBUG"/>
                          …
                        </option>
                      </tool>
                      <tool name="MCU/MPU GCC Compiler"> … </tool>
                    </toolChain>
                  </folderInfo>
                </configuration>
              </storageModule>
            </cconfiguration>

            <cconfiguration id="…release…"> … </cconfiguration>
          </storageModule>
        </cproject>

    We union all includePath and definedSymbols entries found across *every*
    tool within a configuration.  This is important because the Assembler and
    C Compiler tool sections sometimes carry different subsets of the full
    include list.  Unioning them guarantees IntelliSense sees everything.
    """
    log = logging.getLogger(__name__)

    tree = ET.parse(cproject_path)
    root = tree.getroot()

    # Locate the top-level settings module that contains all cconfiguration
    # blocks.  We use a direct child search (no leading "//") to avoid
    # accidentally matching the same moduleId on nested elements.
    outer = root.find("storageModule[@moduleId='org.eclipse.cdt.core.settings']")
    if outer is None:
        raise ValueError(
            f"Cannot find the outer settings storageModule in {cproject_path}.\n"
            "Is this a valid Eclipse CDT .cproject file?"
        )

    configs: list[EclipseConfig] = []

    for cconfig in outer.findall("cconfiguration"):

        # The human-readable name lives in the inner storageModule that also
        # carries moduleId="org.eclipse.cdt.core.settings".  It is a direct
        # child of the <cconfiguration> element and has a 'name' attribute
        # like "Debug" or "Release".
        name_module = cconfig.find(
            "storageModule[@moduleId='org.eclipse.cdt.core.settings']"
        )
        config_name = (
            name_module.get("name", "Unknown")
            if name_module is not None
            else "Unknown"
        )

        log.info("Parsing Eclipse configuration: '%s'", config_name)

        cfg = EclipseConfig(name=config_name)

        # Accumulate raw path strings and symbol strings from every <option>
        # in this cconfiguration, regardless of which tool they belong to.
        # Using plain lists (not sets) preserves original ordering so that
        # intentional include-path priorities are respected.
        raw_paths:   list[str] = []
        raw_defines: list[str] = []

        # cconfig.iter("option") performs a depth-first walk that visits every
        # <option> element anywhere inside this <cconfiguration> block —
        # including those nested inside <toolChain>, <tool>, etc.
        for option in cconfig.iter("option"):
            vtype = option.get("valueType", "")

            if vtype == "includePath":
                values = _collect_list_values(option)
                log.debug(
                    "  includePath  [%s]: %d entries",
                    option.get("superClass", "?"), len(values),
                )
                raw_paths.extend(values)

            elif vtype == "definedSymbols":
                values = _collect_list_values(option)
                log.debug(
                    "  definedSymbols [%s]: %d entries",
                    option.get("superClass", "?"), len(values),
                )
                raw_defines.extend(values)

            # MCU part number — a plain string option, not a list.
            # We only record the first occurrence; all tools reference the same
            # toolchain-level option so it should be identical everywhere.
            elif _superclass_contains(option, _SC_MCU) and cfg.mcu is None:
                cfg.mcu = option.get("value")
                log.debug("  MCU: %s", cfg.mcu)

            # FPU — enumerated value like "…option.fpu.value.fpv5-sp-d16".
            # "no" and "none" mean software float; treat them as absent.
            elif _superclass_contains(option, _SC_FPU) and cfg.fpu is None:
                raw_val  = option.get("value", "")
                friendly = _extract_enum_value(raw_val)
                if friendly not in ("", "no", "none"):
                    cfg.fpu = friendly
                log.debug("  FPU: '%s' → %s", raw_val, cfg.fpu)

            # Float ABI — enumerated value like "…option.floatabi.value.hard".
            elif _superclass_contains(option, _SC_FLOATABI) and cfg.float_abi is None:
                raw_val  = option.get("value", "")
                friendly = _extract_enum_value(raw_val)
                if friendly not in ("", "default"):
                    cfg.float_abi = friendly
                log.debug("  float ABI: '%s' → %s", raw_val, cfg.float_abi)

        # Convert raw "../Foo" paths to "${workspaceFolder}/Foo" then de-dup.
        cfg.include_paths = _ordered_unique(
            [_convert_path(p) for p in raw_paths]
        )
        # De-duplicate defines, preserving first-seen order.
        cfg.defines = _ordered_unique(raw_defines)

        log.info(
            "  → %d include paths | %d defines | MCU=%s | FPU=%s | ABI=%s",
            len(cfg.include_paths), len(cfg.defines),
            cfg.mcu, cfg.fpu, cfg.float_abi,
        )

        configs.append(cfg)

    return configs


# Compiler-flag derivation

def mcu_to_cpu(mcu: Optional[str]) -> Optional[str]:
    """
    Map an STM32 MCU part-number string to the GCC -mcpu= target name.

    We compare the upper-cased MCU string against each prefix in
    MCU_PREFIX_TO_CPU (most-specific first) and return the first match.

    Returns None if no prefix matches so the caller can warn the user rather
    than silently emitting a wrong -mcpu flag.
    """
    if not mcu:
        return None

    mcu_upper = mcu.upper()

    for prefix, cpu_name in MCU_PREFIX_TO_CPU:
        if mcu_upper.startswith(prefix):
            return cpu_name

    return None  # unknown family — caller decides how to handle this


def build_compiler_args(cfg: EclipseConfig) -> list[str]:
    """
    Derive the compilerArgs list for a VS Code IntelliSense configuration.

    These flags tell the IntelliSense engine how the code will actually be
    compiled, which affects:
      -mcpu   : which instruction extensions are legal (ex. DSP, FP)
      -mthumb : Thumb/Thumb-2 instruction encoding (required for all Cortex-M)
      -mfpu   : which FPU hardware registers and instructions are available
      -mfloat-abi : whether floats travel in FPU registers (hard) or integer
                    registers (softfp/soft); affects function call ABI

    Getting these wrong causes IntelliSense to red-underline valid intrinsics
    or accept invalid ones.
    """
    log = logging.getLogger(__name__)

    # Start from the flags that apply to every ARM Cortex-M target.
    args: list[str] = list(BASE_COMPILER_ARGS)

    cpu = mcu_to_cpu(cfg.mcu)
    if cpu:
        args.append(f"-mcpu={cpu}")
    else:
        log.warning(
            "Unknown MCU '%s' — omitting -mcpu flag.  "
            "Add an entry to MCU_PREFIX_TO_CPU at the top of this script.",
            cfg.mcu,
        )

    if cfg.fpu:
        args.append(f"-mfpu={cfg.fpu}")

    if cfg.float_abi:
        args.append(f"-mfloat-abi={cfg.float_abi}")

    return args


# VS Code JSON helpers

def load_existing_props(props_path: Path) -> dict:
    """
    Load the current c_cpp_properties.json, returning an empty dict on failure.

    We read the existing file *only* to preserve settings that have no
    representation in .cproject (compilerPath, cStandard, cppStandard,
    intelliSenseMode).  Those are usually set once by the developer and should
    not be clobbered on every sync.

    If the file is missing or malformed we fall back to the FALLBACK_* constants
    defined at the top of this script.
    """
    log = logging.getLogger(__name__)

    if not props_path.exists():
        log.warning(
            "%s does not exist — falling back to built-in defaults.", props_path.name
        )
        return {}

    try:
        with props_path.open(encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        log.warning(
            "Could not read %s (%s) — falling back to built-in defaults.",
            props_path.name, exc,
        )
        return {}


def _preserve(existing_configs: list[dict], key: str, fallback: str) -> str:
    """
    Return the value of *key* from the first existing configuration that
    defines it, or *fallback* if none do.

    This is how we carry forward user-edited settings (like a custom absolute
    compilerPath) without overwriting them on every sync.
    """
    for cfg in existing_configs:
        if key in cfg:
            return cfg[key]
    return fallback


def eclipse_config_to_vscode(
    eclipse_cfg: EclipseConfig,
    existing_configs: list[dict],
) -> dict:
    """
    Convert one EclipseConfig to a VS Code c_cpp_properties configuration dict.

    The VS Code C/C++ extension schema reference is at:
    https://code.visualstudio.com/docs/cpp/c-cpp-properties-schema-reference

    Fields sourced from .cproject (always overwritten to stay in sync):
      name, includePath, defines, compilerArgs

    Fields preserved from the existing c_cpp_properties.json (user-editable):
      compilerPath, cStandard, cppStandard, intelliSenseMode
    """
    return {
        # Configuration name shown in the VS Code status bar.  Using the
        # Eclipse name (Debug / Release) lets the user switch IntelliSense
        # context to match whichever build they are working on.
        "name":             eclipse_cfg.name,

        # Include search paths — fully overwritten from .cproject on every run.
        "includePath":      eclipse_cfg.include_paths,

        # Preprocessor defines — fully overwritten from .cproject on every run.
        "defines":          eclipse_cfg.defines,

        # Compiler executable — preserved from existing file; falls back to the
        # bare executable name so the system PATH is searched.
        "compilerPath":     _preserve(existing_configs, "compilerPath",     FALLBACK_COMPILER_PATH),

        # Language standard — preserved; IntelliSense uses this to decide which
        # built-in keywords and features are available.
        "cStandard":        _preserve(existing_configs, "cStandard",        FALLBACK_C_STANDARD),
        "cppStandard":      _preserve(existing_configs, "cppStandard",      FALLBACK_CPP_STANDARD),

        # IntelliSense engine variant — "gcc-arm" makes the extension behave
        # as if it is parsing for an ARM GCC target.
        "intelliSenseMode": _preserve(existing_configs, "intelliSenseMode", FALLBACK_INTELLISENSE),

        # Architecture flags — derived fresh from .cproject each run.
        "compilerArgs":     build_compiler_args(eclipse_cfg),
    }


def build_props_json(
    eclipse_configs: list[EclipseConfig],
    existing_props: dict,
) -> dict:
    """
    Assemble the complete c_cpp_properties.json dict.

    One VS Code configuration entry is emitted for every Eclipse build
    configuration found in .cproject.  This means the generated file will
    typically have "Debug" and "Release" entries, which VS Code lets you
    toggle in the status bar while a C file is open.
    """
    existing_configurations: list[dict] = existing_props.get("configurations", [])

    return {
        "configurations": [
            eclipse_config_to_vscode(ec, existing_configurations)
            for ec in eclipse_configs
        ],
        "version": CPP_PROPS_VERSION,
    }


# File I/O

def backup_file(path: Path) -> Optional[Path]:
    """
    Copy *path* to *path*.BAK-YYYYMMDD-HHMMSS and return the new path.

    Returns None without raising if the source does not exist — on a fresh
    clone there is nothing to back up, and that is fine.

    shutil.copy2 preserves the original file's metadata (timestamps,
    permissions) in the backup, which is good housekeeping.
    """
    if not path.exists():
        return None

    timestamp   = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = path.parent / (path.name + f".BAK-{timestamp}")

    shutil.copy2(path, backup_path)
    logging.getLogger(__name__).info(
        "Backed up  %s  →  %s", path.name, backup_path.name
    )

    return backup_path


def write_json(path: Path, data: dict) -> None:
    """
    Write *data* as pretty-printed JSON to *path*.

    Creates any missing parent directories so the script works even if the
    .vscode/ directory does not yet exist.

    The trailing newline is intentional: most editors and git diff tools
    display a warning when a file does not end with one.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=4)
        fh.write("\n")


# CLI and entry point

def _build_arg_parser() -> argparse.ArgumentParser:
    """
    Construct and return the argument parser.

    Kept in its own function so main() stays readable and tests can
    instantiate the parser without running the full program.
    """
    # add_help=False disables the automatic -h/--help so we can register --h
    # and --help ourselves below.
    parser = argparse.ArgumentParser(
        prog="stm32cube_to_vscode_sync.py",
        add_help=False,
        description=(
            "Sync STM32CubeIDE build settings into VS Code IntelliSense.\n"
            "\n"
            "Reads the Eclipse CDT .cproject XML file and writes a fresh\n"
            ".vscode/c_cpp_properties.json containing the include paths,\n"
            "preprocessor defines, and compiler flags that match the IDE build.\n"
            "The existing c_cpp_properties.json is backed up with a timestamp\n"
            "before anything is overwritten."
        ),
        epilog=(
            "examples:\n"
            "  # Run from the project root (. means current directory):\n"
            "  python3 Middlewares/stm32cube_to_vscode_sync.py --project-root .\n"
            "\n"
            "  # Run from anywhere using an absolute path:\n"
            "  python3 Middlewares/stm32cube_to_vscode_sync.py --project-root ~/projects/my-stm32\n"
            "\n"
            "  # Preview what would be written without touching any files:\n"
            "  python3 Middlewares/stm32cube_to_vscode_sync.py --project-root . --dry-run\n"
            "\n"
            "  # Show detailed per-option parse output for debugging:\n"
            "  python3 Middlewares/stm32cube_to_vscode_sync.py --project-root . --verbose\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--h", "--help",
        action="help",
        default=argparse.SUPPRESS,
        help="Show this help message and exit.",
    )
    parser.add_argument(
        "--project-root",
        metavar="PATH",
        type=Path,
        required=True,
        help=(
            "Path to the STM32CubeIDE project root — the directory that contains "
            ".cproject and the .vscode/ folder.  "
            "Accepts an absolute path (/home/user/my-stm32) or a relative path "
            "(. or ../my-stm32) resolved from the current working directory."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Parse .cproject and print the generated c_cpp_properties.json to "
            "stdout, then exit without writing or backing up anything.  "
            "Use this to preview the output or pipe it into another tool before "
            "committing to an actual update."
        ),
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help=(
            "Enable DEBUG-level log output.  "
            "Prints every <option> element that is matched during XML parsing, "
            "including how many include paths and defines were found in each tool "
            "section.  Useful when a path or define is missing from the output "
            "and you need to trace where it should have come from in .cproject."
        ),
    )
    return parser


def resolve_project_root(cli_root: Path) -> Path:
    """
    Resolve --project-root to an absolute path.

    Path.resolve() expands relative paths against the current working directory
    and eliminates any ".." components, so the rest of the script always works
    with a clean absolute path regardless of where the user invoked it from.
    """
    return cli_root.resolve()


def main() -> int:
    """
    Orchestrate the full sync pipeline and return a shell exit code.

    Returning an integer (rather than calling sys.exit directly) makes it easy
    to call main() from a test or wrapper script and inspect the result.

    Exit codes:
        0 — success
        1 — unrecoverable error (details logged to stderr)
    """
    args = _build_arg_parser().parse_args()

    # Configure the logging module.  INFO shows normal progress; DEBUG shows
    # per-option detail useful when diagnosing unexpected output.
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-8s  %(message)s",
        stream=sys.stderr,     # keep stdout clean for --dry-run JSON output
    )
    log = logging.getLogger(__name__)

    # Resolve all paths up-front so every subsequent operation uses absolute
    # paths and we never accidentally operate on the wrong directory.
    project_root = resolve_project_root(args.project_root)
    cproject_path = project_root / CPROJECT_FILENAME
    props_path    = project_root / VSCODE_DIR / CPP_PROPS_FILENAME

    log.info("Project root : %s", project_root)
    log.info("Source       : %s", cproject_path)
    log.info("Target       : %s", props_path)

    # Guard: the source file must exist
    if not cproject_path.exists():
        log.error(
            "Cannot find '%s'.\n"
            "If the project root is not the parent of Middlewares/, "
            "pass --project-root explicitly.",
            cproject_path,
        )
        return 1

    # Phase 1: parse .cproject
    log.info("Parsing %s", CPROJECT_FILENAME)
    try:
        eclipse_configs = parse_cproject(cproject_path)
    except ET.ParseError as exc:
        log.error("XML parse error in %s: %s", cproject_path, exc)
        return 1
    except ValueError as exc:
        log.error("%s", exc)
        return 1

    if not eclipse_configs:
        log.error(
            "No build configurations found in %s — the file may be empty or malformed.",
            cproject_path,
        )
        return 1

    # Phase 2: load existing JSON to preserve user-edited values
    log.info("Reading existing %s", CPP_PROPS_FILENAME)
    existing_props = load_existing_props(props_path)

    # Phase 3: build the new JSON structure in memory
    log.info("Building new configuration")
    new_props = build_props_json(eclipse_configs, existing_props)

    # Dry-run: print and exit without touching files
    if args.dry_run:
        # Print to stdout so the caller can redirect / pipe it.
        print(json.dumps(new_props, indent=4))
        log.info("Dry run — no files written.")
        return 0

    # Phase 4: back up the existing file
    log.info("Backing up existing file")
    backup_file(props_path)

    # Phase 5: write the new file
    log.info("Writing %s", CPP_PROPS_FILENAME)
    try:
        write_json(props_path, new_props)
    except OSError as exc:
        log.error("Failed to write %s: %s", props_path, exc)
        return 1

    log.info(
        "Done — wrote %d configuration(s) to %s.",
        len(new_props["configurations"]), props_path,
    )
    return 0


# The `if __name__ == "__main__"` guard means this file can also be imported
# as a module without side effects — useful if you later want to call
# parse_cproject() or build_props_json() from another script.
if __name__ == "__main__":
    sys.exit(main())
