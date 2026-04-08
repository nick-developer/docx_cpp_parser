"""
setup.py for docx_comment_parser Python extension.

Build & install:
    pip install pybind11
    pip install .

Or build in-place:
    python setup.py build_ext --inplace

Supported toolchains:
    Linux / macOS  : GCC or Clang          (uses -std=c++17, -lz)
    Windows MSVC   : Visual Studio 2019+   (uses /std:c++17)
    Windows MinGW  : MinGW-w64 via MSYS2   (uses -std=c++17, -lz)
"""

from setuptools import setup, Extension
import sys
import os
import subprocess

# Absolute path to the directory containing this file, so include_dirs and
# sources resolve correctly regardless of the working directory pip uses.
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
    # MSVC (cl.exe) — Visual Studio 2019 or later
    extra_compile_args = [
        "/std:c++17",
        "/O2",
        "/DNDEBUG",
        "/EHsc",                    # enable C++ exception handling
        "/DDOCX_BUILDING_DLL",      # expand DOCX_API to __declspec(dllexport)
    ]
    extra_link_args = []            # zlib linked via vcpkg / find_package

elif IS_MINGW:
    # MinGW-w64 GCC on Windows (MSYS2 / standalone)
    extra_compile_args = [
        "-std=c++17",
        "-O2",
        "-DNDEBUG",
        "-DDOCX_BUILDING_DLL",      # expand DOCX_API to __declspec(dllexport)
        "-Wall",
    ]
    extra_link_args = [
        "-lz",                      # zlib1.dll — ships with every MinGW installation
        "-lws2_32",                 # Winsock — required by std::thread on MinGW
        "-lmswsock",
    ]

else:
    # Linux / macOS — GCC or Clang
    extra_compile_args = [
        "-std=c++17",
        "-O3",
        "-DNDEBUG",
        "-fvisibility=hidden",      # hide all symbols except those marked DOCX_API
    ]
    extra_link_args = ["-lz"]

# ─── Source files ─────────────────────────────────────────────────────────────
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
        os.path.join(ROOT, "vendor"),
        pybind11_include,
    ],
    extra_compile_args=extra_compile_args,
    extra_link_args=extra_link_args,
    language="c++",
)

setup(
    name="docx-comment-parser",
    version="1.0.0",
    author="nick-developer",
    description="Fast C++ library for extracting comment metadata from .docx files",
    long_description=open(os.path.join(ROOT, "README.md"), encoding="utf-8").read() if os.path.exists(os.path.join(ROOT, "README.md")) else "",
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
