"""
setup.py for docx_comment_parser Python extension.

This file is kept as a fallback for environments that cannot use scikit-build-core.
The recommended build path is:

    pip install scikit-build-core pybind11
    pip install .            # uses pyproject.toml → scikit-build-core → CMake

For a manual setuptools build (no CMake required):

    pip install pybind11
    pip install .             # setuptools falls back to this file
    # or in-place:
    python setup.py build_ext --inplace

Supported toolchains:
    Linux / macOS  : GCC or Clang          (uses -std=c++17, -lz)
    Windows MSVC   : Visual Studio 2019+   (uses /std:c++17, vendored zlib)
    Windows MinGW  : MinGW-w64 via MSYS2   (uses -std=c++17, -lz)
"""

from setuptools import setup, Extension
import sys
import os
import subprocess

ROOT = os.path.dirname(os.path.abspath(__file__))


def _is_mingw() -> bool:
    """Return True when building with MinGW-w64 GCC under Windows."""
    if sys.platform != "win32":
        return False
    try:
        out = subprocess.check_output(
            ["gcc", "--version"], stderr=subprocess.DEVNULL
        ).decode()
        return "mingw" in out.lower() or "MINGW" in out
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


# ─── Locate pybind11 headers ──────────────────────────────────────────────────
try:
    import pybind11
    pybind11_include = pybind11.get_include()
except ImportError:
    raise RuntimeError("pybind11 not found. Install with: pip install pybind11")

# ─── Platform / toolchain flags ──────────────────────────────────────────────
IS_MINGW = _is_mingw()
IS_MSVC  = sys.platform == "win32" and not IS_MINGW

if IS_MSVC:
    # MSVC (cl.exe) — Visual Studio 2019 or later.
    # zlib is provided by the vendored single-header in vendor/zlib/zlib.h
    # (activated by VENDOR_ZLIB_IMPLEMENTATION inside zip_reader.cpp), so no
    # external -lz is needed.
    extra_compile_args = [
        "/std:c++17",
        "/O2",
        "/DNDEBUG",
        "/EHsc",                    # enable C++ exception handling
        "/DDOCX_BUILDING_DLL",      # DOCX_API → __declspec(dllexport)
    ]
    extra_link_args = []

elif IS_MINGW:
    # MinGW-w64 GCC on Windows (MSYS2 / standalone).
    # Notes:
    #   - DOCX_BUILDING_DLL is correct here because setup.py compiles ALL C++
    #     sources directly into the single .pyd extension (not a separate DLL),
    #     so the extension IS the thing that "builds" the symbols.
    #   - -lmswsock is intentionally omitted: it is not needed for this library
    #     and is absent from some MinGW-w64 installations, causing link failure.
    #   - -lws2_32 is likewise unnecessary (no socket code in this library).
    extra_compile_args = [
        "-std=c++17",
        "-O2",
        "-DNDEBUG",
        "-DDOCX_BUILDING_DLL",
        "-Wall",
    ]
    extra_link_args = [
        "-lz",                      # zlib1.dll — ships with every MinGW installation
    ]

else:
    # Linux / macOS — GCC or Clang
    extra_compile_args = [
        "-std=c++17",
        "-O3",
        "-DNDEBUG",
        "-fvisibility=hidden",
        "-DDOCX_BUILDING_DLL",
    ]
    extra_link_args = ["-lz"]

# ─── Source files ─────────────────────────────────────────────────────────────
# All C++ sources are compiled directly into the single Python extension — there
# is no separate docx_comment_parser.dll/so to distribute alongside the wheel.
sources = [
    os.path.join(ROOT, "python", "python_bindings.cpp"),
    os.path.join(ROOT, "src", "docx_parser.cpp"),
    os.path.join(ROOT, "src", "batch_parser.cpp"),
    os.path.join(ROOT, "src", "zip_reader.cpp"),
    os.path.join(ROOT, "src", "xml_parser.cpp"),
]

ext = Extension(
    "docx_comment_parser",
    sources=sources,
    include_dirs=[
        os.path.join(ROOT, "include"),
        os.path.join(ROOT, "vendor"),   # vendored zlib.h for MSVC
        pybind11_include,
    ],
    extra_compile_args=extra_compile_args,
    extra_link_args=extra_link_args,
    language="c++",
)

setup(
    name="docx-comment-parser",
    version="1.1.1",
    author="nick-developer",
    description="Fast C++ library for extracting comment metadata from .docx files",
    long_description=(
        open(os.path.join(ROOT, "README.md"), encoding="utf-8").read()
        if os.path.exists(os.path.join(ROOT, "README.md"))
        else ""
    ),
    ext_modules=[ext],
    python_requires=">=3.8",
    classifiers=[
        "Programming Language :: Python :: 3",
        "Programming Language :: C++",
        "Operating System :: POSIX :: Linux",
        "Operating System :: Microsoft :: Windows",
        "Operating System :: MacOS",
    ],
)
