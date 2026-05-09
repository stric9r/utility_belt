# CLAUDE.md — Global

This file provides global guidance to Claude Code across all projects.
Project-specific build instructions and architecture live in each repo's
own CLAUDE.md.

If not set, ask the author their name for the template header documentation.

# Commands
Any command that does not modify, delete, or add to the system or files
can be run without permission.

# Coding Standards

## Coding standard
Follow Google's coding standard
https://google.github.io/styleguide/cppguide.html.
It's for C++ but can be applied to C. Only deviate based on rules listed
in this markdown.

## Line length
Do not exceed 80 characters per line, including spaces.

## Tabs
No tabs, only spaces. The tab space shall be 4 spaces.
If tabs are encountered outside of vendor/generated directories, convert
them to spaces.

## Naming

- No `s_` prefix on static variables.
- Bool variables use `b` prefix: `bool bContinue`, `bool bUsbActive`.
- Pointer variables use `p` prefix: `uint8_t *pBuffer`, `Node *pNode`.
- No prefix for other variables: `slaveAddr`, `usbPendingByte`.
- `const` goes after the type: `uint16_t const`, not `const uint16_t`.
- Prefer camelCase for new identifiers. When editing existing files,
  default to the naming convention already in use in that module.

## Comparisons

- Yoda conditions for `==` and `!=`: put the constant/literal on the left
  so accidental assignment is a compile error.
  - Correct: `if (0u == usbTxLen)`, `if (USBD_BUSY == result)`
  - Wrong:   `if (usbTxLen == 0u)`, `if (result == USBD_BUSY)`

## Control flow

- No multiple returns in a single function.
- Use `bool bContinue = true` for sequential guard checks:
  ```c
  bool bContinue = true;
  eMBErrorCode errorCode = MB_ENOERR;

  if (bContinue && someCheck) { bContinue = false; }
  if (bContinue && anotherCheck) {
      bContinue = false;
      errorCode = MB_ENOERR;
  }
  if (bContinue) { doWork(); }

  return errorCode;
  ```

## Headers and coupling

- No raw `extern` declarations inside `.c` files. Expose the symbol in a
  header and include the header instead.
- Resource handles are private to their owning `.c` file. Expose them only
  through a getter function.
- ISR handlers belong in the same `.c` file that defines the handle they
  operate on.
- All headers must have an include guard and `extern "C"` block.
- Avoid cross-coupling modules. Dependencies should be explicit through
  header includes.

## Comments

- Doxygen only in `.c` files. Headers contain declarations only (no
  function docs) unless it is a macro, struct, or enum defined in the
  header.
- When a function intentionally does nothing, always explain why with a
  comment.
- Comment the WHY, not the WHAT. Code reads itself; readers need context.

## File headers

Every `.c` and `.h` file carries an attribution block. Apply the correct
template based on the file's origin:

**Template A — files that originated from a vendor**

Append immediately after the closing `*/` of the vendor doxygen block.
Do not touch the vendor block itself.

```c
/*
 * Modified by Stric Roberts, 2026.
 * MIT License — see LICENSE in the project root.
 */
```

**Template B — files authored by Stric Roberts (no vendor block)**

Prepend at the very top of the file, before all other content.

```c
/**
  ******************************************************************************
  * @file           : filename.c
  * @brief          : One-line description (≤ 58 chars so full line ≤ 80)
  ******************************************************************************
  * [Author's name], 2026. MIT License — see LICENSE in the project root.
  ******************************************************************************
  */
```

If the `@brief` text exceeds 58 characters, wrap it onto a continuation
line aligned at column 22 (matching the start of the brief text):

```c
  * @brief          : First part of the description that wraps
  *                   continuation aligned here
```

Files with a hand-written header block containing neither the vendor name
nor "Stric Roberts" get a standalone ownership banner prepended:

```c
/**
  ******************************************************************************
  * [Author's name], 2026. MIT License — see LICENSE in the project root.
  ******************************************************************************
  */
```

## Todos

Format: `// @todo [YYYY-MM-DD] Description`
If an issue tracker number is available, include it after the date:
`// @todo [YYYY-MM-DD] #123 Description`

When adding a todo, ask: "Is there an issue number to associate with this?"

## Workflow

- Always explain a proposed code change before making any edits. Wait for
  explicit approval before writing to any file.

## Git commits

- Do not add `Co-Authored-By: Claude` trailers to commit messages.
  Credit is given in README.md already.
