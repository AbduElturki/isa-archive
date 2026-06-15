import re
import shutil
import subprocess
import pathlib
import logging

import jinja2

logger = logging.getLogger("isa_archive.generators")

# C/C++ file extensions clang-format understands. ``.inc`` is C included verbatim
# into a translation unit, so it is formatted with --assume-filename. TableGen
# (.td), decodetree (.decode), Markdown, shell, etc. are left to the normalizer.
_C_LIKE = {".c", ".h", ".cpp", ".cc", ".cxx", ".hpp"}
# Clamp runs of >2 blank lines to 2, which keeps egregious template gaps out
# while preserving the two-blank-line separation Python (PEP 8) wants between
# top-level defs in the generated assembler.
_BLANK_RUN = re.compile(r"\n{4,}")


def make_jinja_env() -> jinja2.Environment:
    return jinja2.Environment(
        loader=jinja2.PackageLoader("isa_archive", "generators/templates"),
        autoescape=jinja2.select_autoescape(),
    )


def prepare_output_dir(output_dir: str) -> pathlib.Path:
    path = pathlib.Path(output_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def normalize_generated(text: str) -> str:
    """Deterministic whitespace cleanup applied to every generated file.

    Templates accumulate trailing whitespace and blank-line runs from loop/
    conditional expansion; this gives all generated artifacts a consistent,
    diff-friendly shape without per-template whitespace fiddling:
      * strip trailing whitespace from every line,
      * clamp any run of more than 2 blank lines to 2,
      * end with exactly one trailing newline.
    """
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    text = _BLANK_RUN.sub("\n\n\n", text)
    return text.rstrip("\n") + "\n"


def _clang_format_in_place(path: pathlib.Path) -> None:
    """Run ``clang-format -i`` on a just-written C/C++ file if the tool exists,
    using ``-style=file`` so the tree's shipped ``.clang-format`` is honored
    (falling back to clang-format's built-in LLVM style if none is found).
    A no-op when clang-format isn't installed — never a hard dependency."""
    exe = shutil.which("clang-format")
    if exe is None:
        return
    assume = path.with_suffix(".c") if path.suffix == ".inc" else path
    try:
        subprocess.run(
            [exe, "-i", "-style=file", f"-assume-filename={assume}", str(path)],
            capture_output=True, text=True, check=True,
        )
    except (subprocess.CalledProcessError, OSError) as e:  # pragma: no cover
        logger.warning("clang-format failed on %s; left unformatted (%s)", path, e)


def write_generated(path, text: str, *, clang_format: bool = False) -> None:
    """Normalize (and optionally clang-format C/C++) then write a generated file.

    The single funnel every generator writes through, so output-quality rules
    live in one place. ``clang_format`` is opt-in (the ``--format`` CLI flag);
    when off, the generated tree still ships a ``.clang-format`` so an adopter's
    editor/CI formats it consistently.
    """
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        f.write(normalize_generated(text))
    if clang_format and (path.suffix.lower() in _C_LIKE or path.suffix.lower() == ".inc"):
        _clang_format_in_place(path)


# clang-format configs shipped into generated trees so adopted code formats to the
# house style each project actually uses: QEMU is LLVM-based but 4-space indented
# (this mirrors QEMU's own .clang-format); LLVM is plain LLVM style (2-space).
CLANG_FORMAT_QEMU = """\
# Shipped by ISA-Archive. Mirrors QEMU's house style (LLVM base, 4-space indent).
# SortIncludes is off because QEMU requires "qemu/osdep.h" to stay first.
BasedOnStyle: LLVM
IndentWidth: 4
SortIncludes: false
"""

CLANG_FORMAT_LLVM = """\
# Shipped by ISA-Archive so this backend formats to LLVM house style.
BasedOnStyle: LLVM
"""


def make_renderer(env, ctx: dict, *, clang_format: bool = False):
    """Return a ``render(template_name, dest)`` callable that renders a Jinja
    template with ``ctx`` and writes it through ``write_generated``. Replaces the
    identical render-to closure each generator used to define."""
    def render(template_name: str, dest) -> None:
        content = env.get_template(template_name).render(**ctx)
        write_generated(dest, content, clang_format=clang_format)
    return render
