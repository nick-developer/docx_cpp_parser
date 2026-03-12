"""
setup.py for docx_comment_parser Python extension.

Build & install:
    pip install pybind11
    pip install .

Or build in-place:
    python setup.py build_ext --inplace
"""

from setuptools import setup, Extension
import sys
import os

# ─── Locate pybind11 headers ──────────────────────────────────────────────────
try:
    import pybind11
    pybind11_include = pybind11.get_include()
except ImportError:
    raise RuntimeError("pybind11 not found. Install with: pip install pybind11")

# ─── Platform-specific flags ─────────────────────────────────────────────────
if sys.platform == "win32":
    extra_compile_args = ["/std:c++17", "/O2", "/DNDEBUG", "/EHsc"]
    extra_link_args    = []
else:
    extra_compile_args = [
        "-std=c++17",
        "-O3",
        "-DNDEBUG",
        "-fvisibility=hidden",
    ]
    extra_link_args = ["-lz"]

# ─── Source files ─────────────────────────────────────────────────────────────
sources = [
    "python/python_bindings.cpp",
    "src/docx_parser.cpp",
    "src/batch_parser.cpp",
    "src/zip_reader.cpp",
    "src/xml_parser.cpp",
]

ext = Extension(
    "docx_comment_parser",
    sources=sources,
    include_dirs=["include", pybind11_include],
    extra_compile_args=extra_compile_args,
    extra_link_args=extra_link_args,
    language="c++",
)

setup(
    name="docx-comment-parser",
    version="1.0.0",
    author="Your Name",
    description="Fast C++ library for extracting comment metadata from .docx files",
    long_description=open("README.md").read() if os.path.exists("README.md") else "",
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
