cmake_minimum_required(VERSION 3.15)
project(docx_comment_parser VERSION 1.0.0 LANGUAGES CXX)

# ─── Policy: normalise install() DESTINATION paths (fixes CMP0177 warning) ───
# Python3_SITEARCH on MinGW contains backslashes which CMake 3.26+ normalises
# automatically under the NEW policy.  Setting it explicitly silences the warning
# on all CMake versions ≥ 3.15 that support CMP0177.
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

# ─── Dependencies ─────────────────────────────────────────────────────────────
# No libxml2 required — self-contained XML parser.
#
# zlib strategy:
#   MSVC   — no system zlib exists.  zip_reader.cpp compiles the vendored
#             single-header inflate from vendor/zlib/zlib.h; no link step needed.
#   MinGW  — zlib1.dll + libz.a ship with every MinGW-w64 installation.
#   Linux / macOS — system zlib (apt install zlib1g-dev / brew install zlib).
if(NOT MSVC)
  find_package(ZLIB REQUIRED)
endif()

# ─── Core shared library ──────────────────────────────────────────────────────
add_library(docx_comment_parser SHARED
    src/docx_parser.cpp
    src/batch_parser.cpp
    src/zip_reader.cpp
    src/xml_parser.cpp
)

# Tell the compiler which TUs are *building* the DLL so DOCX_API expands to
# dllexport; consumers that only include the header get dllimport instead.
target_compile_definitions(docx_comment_parser PRIVATE DOCX_BUILDING_DLL)

target_include_directories(docx_comment_parser
    PUBLIC
        $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
        $<INSTALL_INTERFACE:include>
    PRIVATE
        # On MSVC the vendored zlib.h is included by zip_reader.cpp via a
        # relative path "../vendor/zlib/zlib.h".  Adding vendor/ here makes
        # the path work regardless of which directory cl.exe is invoked from.
        $<$<CXX_COMPILER_ID:MSVC>:${CMAKE_CURRENT_SOURCE_DIR}/vendor>
)

target_link_libraries(docx_comment_parser
    PRIVATE
        # Link system zlib on non-MSVC platforms only.
        # On MSVC, inflate is compiled directly into zip_reader.cpp.
        $<$<NOT:$<CXX_COMPILER_ID:MSVC>>:ZLIB::ZLIB>
)

# On MinGW, link the standard threading and networking libs that std::thread
# and socket code pull in indirectly.
if(MINGW)
  target_link_libraries(docx_comment_parser PRIVATE -lws2_32 -lmswsock)
endif()

# Symbol visibility — ELF only.  On Windows (PE/DLL) visibility is handled by
# __declspec(dllexport/dllimport) in the header; the GCC attribute is a no-op
# there but can confuse some linker versions, so guard it explicitly.
if(NOT WIN32)
  set_target_properties(docx_comment_parser PROPERTIES
      CXX_VISIBILITY_PRESET  hidden
      VISIBILITY_INLINES_HIDDEN ON
  )
endif()

# VERSION / SOVERSION are ELF-only concepts (produce .so.1 symlinks on Linux).
# MinGW/Windows uses a different DLL versioning mechanism; setting these
# properties on a PE target causes the ld "error: ld returned 5" link failure.
if(NOT WIN32)
  set_target_properties(docx_comment_parser PROPERTIES
      VERSION   ${PROJECT_VERSION}
      SOVERSION 1
  )
endif()

target_compile_options(docx_comment_parser PRIVATE
    $<$<CXX_COMPILER_ID:GNU,Clang>:-Wall -Wextra -Wpedantic>
    $<$<CONFIG:Release>:-O3 -DNDEBUG>
)

# ─── Python extension (optional) ──────────────────────────────────────────────
option(BUILD_PYTHON_BINDINGS "Build Python bindings via pybind11" ON)

if(BUILD_PYTHON_BINDINGS)
    find_package(Python3 REQUIRED COMPONENTS Interpreter Development)
    find_package(pybind11 CONFIG QUIET)

    if(NOT pybind11_FOUND)
        # Locate pybind11 installed via pip
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
        pybind11_add_module(_docx_comment_parser
            python/python_bindings.cpp
        )

        # The extension builds *into* the DLL — it also needs DOCX_BUILDING_DLL
        # so that DOCX_API expands to dllexport when compiling its TU on Windows.
        target_compile_definitions(_docx_comment_parser PRIVATE DOCX_BUILDING_DLL)

        target_include_directories(_docx_comment_parser PRIVATE
            ${CMAKE_CURRENT_SOURCE_DIR}/include
            # Vendor dir for MSVC (same reason as the main library target)
            $<$<CXX_COMPILER_ID:MSVC>:${CMAKE_CURRENT_SOURCE_DIR}/vendor>
        )

        target_link_libraries(_docx_comment_parser PRIVATE
            docx_comment_parser
            $<$<NOT:$<CXX_COMPILER_ID:MSVC>>:ZLIB::ZLIB>
        )

        # CMP0177 NEW: CMake normalises the path before passing it to install(),
        # so backslashes from Python3_SITEARCH on Windows are converted to slashes.
        install(TARGETS _docx_comment_parser
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
