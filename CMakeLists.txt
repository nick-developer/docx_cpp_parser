cmake_minimum_required(VERSION 3.15)
project(docx_comment_parser VERSION 1.1.1 LANGUAGES CXX)

# ─── Policy: normalise install() DESTINATION paths (fixes CMP0177 warning) ───
if(POLICY CMP0177)
  cmake_policy(SET CMP0177 NEW)
endif()

# ─── C++ standard ─────────────────────────────────────────────────────────────
set(CMAKE_CXX_STANDARD          17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS        OFF)

if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()

# ─── LTO (Release only, skip on MinGW where it is unreliable) ─────────────────
if(NOT MINGW)
  include(CheckIPOSupported)
  check_ipo_supported(RESULT _ipo_ok OUTPUT _ipo_err)
  if(_ipo_ok)
    set(CMAKE_INTERPROCEDURAL_OPTIMIZATION_RELEASE ON)
  endif()
endif()

# ─── zlib ─────────────────────────────────────────────────────────────────────
# MSVC  — vendored single-header inflate compiled into zip_reader.cpp; no link step.
# MinGW — zlib1.dll + libz.a ship with every MinGW-w64 installation.
# Linux / macOS — system zlib (apt install zlib1g-dev / brew install zlib).
if(NOT MSVC)
  find_package(ZLIB REQUIRED)
endif()

# ─── Common compile options helper ────────────────────────────────────────────
# Applied to every target that compiles the library sources.
function(docx_apply_compile_options target)
  target_compile_options(${target} PRIVATE
      $<$<CXX_COMPILER_ID:GNU,Clang>:-Wall -Wextra -Wpedantic>
      # -O3 / -DNDEBUG only for GCC/Clang; MSVC sets optimisation via build type.
      $<$<AND:$<CONFIG:Release>,$<CXX_COMPILER_ID:GNU,Clang>>:-O3 -DNDEBUG>
  )
endfunction()

# ─── MinGW pthread detection ──────────────────────────────────────────────────
# std::thread on MinGW with the POSIX threading model needs -lpthread.
# With the Win32 model (default in most MSYS2 toolchains) no extra flag is needed.
# -lws2_32 / -lmswsock are NOT needed (no socket code); -lmswsock in particular
# is absent from some MinGW installations and was the previous cause of
# "ld returned 5" (ERROR_ACCESS_DENIED / linker crash).
if(MINGW)
  include(CheckCXXSourceCompiles)
  check_cxx_source_compiles("
    #include <pthread.h>
    int main() { return 0; }
  " _has_pthread_h)
endif()

# ─── Helper: link zlib + pthreads for non-MSVC targets ───────────────────────
function(docx_link_platform_libs target)
  target_link_libraries(${target} PRIVATE
      $<$<NOT:$<CXX_COMPILER_ID:MSVC>>:ZLIB::ZLIB>
  )
  if(MINGW AND _has_pthread_h)
    target_link_libraries(${target} PRIVATE -lpthread)
  endif()
endfunction()

# ═════════════════════════════════════════════════════════════════════════════
#  SCIKIT-BUILD-CORE (wheel) MODE
#  When scikit-build-core drives the build it defines SKBUILD.
#  In this mode we produce a single self-contained Python extension with all
#  C++ sources compiled in — no separate DLL.  This mirrors what setup.py does
#  and avoids the MSVC LNK1149 "output filename matches input filename" error
#  that occurs when both docx_comment_parser.dll and docx_comment_parser.pyd
#  would generate the same docx_comment_parser.lib import library.
# ═════════════════════════════════════════════════════════════════════════════
if(DEFINED SKBUILD)

  find_package(Python3 REQUIRED COMPONENTS Interpreter Development.Module)
  find_package(pybind11 CONFIG QUIET)

  if(NOT pybind11_FOUND)
    execute_process(
        COMMAND ${Python3_EXECUTABLE} -c "import pybind11; print(pybind11.get_cmake_dir())"
        OUTPUT_VARIABLE _pybind11_cmake_dir
        ERROR_QUIET OUTPUT_STRIP_TRAILING_WHITESPACE
    )
    if(_pybind11_cmake_dir)
      list(APPEND CMAKE_PREFIX_PATH "${_pybind11_cmake_dir}")
      find_package(pybind11 CONFIG REQUIRED)
    endif()
  endif()

  # Single module — all sources compiled in, no runtime DLL dependency.
  pybind11_add_module(docx_comment_parser MODULE
      python/python_bindings.cpp
      src/docx_parser.cpp
      src/batch_parser.cpp
      src/zip_reader.cpp
      src/xml_parser.cpp
  )

  # DOCX_BUILDING_DLL is correct here: the extension IS building the symbols
  # (not importing them from a separate DLL).
  target_compile_definitions(docx_comment_parser PRIVATE DOCX_BUILDING_DLL)

  target_include_directories(docx_comment_parser PRIVATE
      ${CMAKE_CURRENT_SOURCE_DIR}/include
      # Vendored zlib.h for MSVC (activated inside zip_reader.cpp)
      $<$<CXX_COMPILER_ID:MSVC>:${CMAKE_CURRENT_SOURCE_DIR}/vendor>
  )

  docx_apply_compile_options(docx_comment_parser)
  docx_link_platform_libs(docx_comment_parser)

  install(TARGETS docx_comment_parser
      DESTINATION "${SKBUILD_PLATLIB_DIR}"
  )

  # Nothing else to build in wheel mode — skip the rest of this file.
  return()

endif()

# ═════════════════════════════════════════════════════════════════════════════
#  REGULAR CMAKE BUILD MODE
#  Builds the standalone shared library + optional Python extension that links
#  against it.  The Python extension uses a distinct CMake target name
#  (docx_py_ext) to avoid colliding with the shared library target, but sets
#  OUTPUT_NAME so the .pyd/.so file is named "docx_comment_parser" to match
#  PYBIND11_MODULE(docx_comment_parser, m) in python_bindings.cpp.
# ═════════════════════════════════════════════════════════════════════════════

# ─── Core shared library ──────────────────────────────────────────────────────
add_library(docx_comment_parser SHARED
    src/docx_parser.cpp
    src/batch_parser.cpp
    src/zip_reader.cpp
    src/xml_parser.cpp
)

target_compile_definitions(docx_comment_parser PRIVATE DOCX_BUILDING_DLL)

target_include_directories(docx_comment_parser
    PUBLIC
        $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
        $<INSTALL_INTERFACE:include>
    PRIVATE
        $<$<CXX_COMPILER_ID:MSVC>:${CMAKE_CURRENT_SOURCE_DIR}/vendor>
)

docx_apply_compile_options(docx_comment_parser)
docx_link_platform_libs(docx_comment_parser)

# Symbol visibility — ELF only.
if(NOT WIN32)
  set_target_properties(docx_comment_parser PROPERTIES
      CXX_VISIBILITY_PRESET     hidden
      VISIBILITY_INLINES_HIDDEN ON
  )
endif()

# VERSION / SOVERSION are ELF-only (.so.1 symlinks on Linux).
# Setting them on a PE/DLL target causes MinGW ld to fail with exit code 5.
if(NOT WIN32)
  set_target_properties(docx_comment_parser PROPERTIES
      VERSION   ${PROJECT_VERSION}
      SOVERSION 1
  )
endif()

# ─── Python extension (optional) ──────────────────────────────────────────────
option(BUILD_PYTHON_BINDINGS "Build Python bindings via pybind11" ON)

if(BUILD_PYTHON_BINDINGS)
    find_package(Python3 REQUIRED COMPONENTS Interpreter Development)
    find_package(pybind11 CONFIG QUIET)

    if(NOT pybind11_FOUND)
        execute_process(
            COMMAND ${Python3_EXECUTABLE} -c "import pybind11; print(pybind11.get_cmake_dir())"
            OUTPUT_VARIABLE _pybind11_cmake_dir
            ERROR_QUIET OUTPUT_STRIP_TRAILING_WHITESPACE
        )
        if(_pybind11_cmake_dir)
            list(APPEND CMAKE_PREFIX_PATH "${_pybind11_cmake_dir}")
            find_package(pybind11 CONFIG REQUIRED)
        endif()
    endif()

    if(pybind11_FOUND)
        # Distinct CMake target name avoids collision with the SHARED library
        # target above.  OUTPUT_NAME makes the file "docx_comment_parser.pyd"
        # so Python's import machinery finds PyInit_docx_comment_parser.
        # (The old name "_docx_comment_parser" caused an ImportError because
        # Python looked for PyInit__docx_comment_parser — note the double
        # underscore — which never existed.)
        pybind11_add_module(docx_py_ext MODULE
            python/python_bindings.cpp
        )

        set_target_properties(docx_py_ext PROPERTIES
            OUTPUT_NAME "docx_comment_parser"
        )

        # Do NOT define DOCX_BUILDING_DLL: this extension links against the
        # shared library above, so DOCX_API must expand to __declspec(dllimport).
        target_include_directories(docx_py_ext PRIVATE
            ${CMAKE_CURRENT_SOURCE_DIR}/include
            $<$<CXX_COMPILER_ID:MSVC>:${CMAKE_CURRENT_SOURCE_DIR}/vendor>
        )

        target_link_libraries(docx_py_ext PRIVATE
            docx_comment_parser
            $<$<NOT:$<CXX_COMPILER_ID:MSVC>>:ZLIB::ZLIB>
        )

        if(MINGW AND _has_pthread_h)
            target_link_libraries(docx_py_ext PRIVATE -lpthread)
        endif()

        install(TARGETS docx_py_ext
            LIBRARY DESTINATION "${Python3_SITEARCH}"
            RUNTIME DESTINATION "${Python3_SITEARCH}"
        )
    else()
        message(WARNING
            "pybind11 not found — Python bindings will not be built.\n"
            "Install with: pip install pybind11")
    endif()
endif()

# ─── Install rules ────────────────────────────────────────────────────────────
include(GNUInstallDirs)

install(TARGETS docx_comment_parser
    EXPORT   docx_comment_parserTargets
    LIBRARY  DESTINATION "${CMAKE_INSTALL_LIBDIR}"
    ARCHIVE  DESTINATION "${CMAKE_INSTALL_LIBDIR}"
    RUNTIME  DESTINATION "${CMAKE_INSTALL_BINDIR}"
    INCLUDES DESTINATION "${CMAKE_INSTALL_INCLUDEDIR}"
)

install(FILES
    include/docx_comment_parser.h
    include/zip_reader.h
    DESTINATION "${CMAKE_INSTALL_INCLUDEDIR}/docx_comment_parser"
)

install(EXPORT docx_comment_parserTargets
    FILE      docx_comment_parserTargets.cmake
    NAMESPACE docx::
    DESTINATION "${CMAKE_INSTALL_LIBDIR}/cmake/docx_comment_parser"
)

# ─── Tests ────────────────────────────────────────────────────────────────────
option(BUILD_TESTS "Build test suite" ON)

if(BUILD_TESTS)
    enable_testing()
    add_subdirectory(tests)
endif()
