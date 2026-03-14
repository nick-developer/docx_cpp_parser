# =============================================================================
# tests/CMakeLists.txt
#
# Builds and registers the self-contained docx_comment_parser test suite.
# Invoked automatically by the parent CMakeLists when BUILD_TESTS=ON (default).
#
# Usage:
#   cmake -B build -DBUILD_TESTS=ON
#   cmake --build build
#   ctest --test-dir build --output-on-failure
# =============================================================================

cmake_minimum_required(VERSION 3.15)

# ─── zlib is needed by the test's own ZIP builder (uses crc32/inflate) ────────
# On MSVC the vendored zlib.h is used (same as zip_reader.cpp).
# On Linux/macOS/MinGW we link the system libz.
if(NOT MSVC)
  find_package(ZLIB REQUIRED)
endif()

# ─── Test executable ──────────────────────────────────────────────────────────
add_executable(test_docx_parser
    test_docx_parser.cpp
)

# Inherit C++17 from parent; make it explicit here as a safety net
set_target_properties(test_docx_parser PROPERTIES
    CXX_STANDARD          17
    CXX_STANDARD_REQUIRED ON
    CXX_EXTENSIONS        OFF
)

target_include_directories(test_docx_parser
    PRIVATE
        # Public headers of the library under test
        ${CMAKE_SOURCE_DIR}/include
        # Vendor dir: test_docx_parser.cpp includes vendor/zlib/zlib.h on MSVC
        $<$<CXX_COMPILER_ID:MSVC>:${CMAKE_SOURCE_DIR}/vendor>
)

target_link_libraries(test_docx_parser
    PRIVATE
        # The shared library being tested (provides docx:: API)
        docx_comment_parser
        # zlib — crc32() is called by the in-memory ZIP builder in the test.
        # On MSVC the vendored header is used; no link step is needed there.
        $<$<NOT:$<CXX_COMPILER_ID:MSVC>>:ZLIB::ZLIB>
)

target_compile_options(test_docx_parser PRIVATE
    $<$<CXX_COMPILER_ID:GNU,Clang>:-Wall -Wextra -Wpedantic>
    # Debug build: keep symbols for readable stack traces
    $<$<CONFIG:Debug>:-g -O0>
    # Release build: full optimisation, suppress debug info
    $<$<CONFIG:Release>:-O2 -DNDEBUG>
)

# ─── CTest registration ───────────────────────────────────────────────────────
# One test entry per logical group so CTest can report them individually.
# All groups are driven by the single binary; the binary's own main() returns
# non-zero on any failure, which CTest treats as a test failure.

add_test(
    NAME    DocxParser.BasicParsing
    COMMAND test_docx_parser
)

# Working directory: project root so that relative paths (e.g. sample .docx
# files placed there in future) resolve correctly.
set_tests_properties(DocxParser.BasicParsing PROPERTIES
    WORKING_DIRECTORY "${CMAKE_SOURCE_DIR}"
    # Fail the test if it takes more than 30 seconds (catches infinite loops)
    TIMEOUT           30
    # Pass these back to CTest's output for easier diagnosis
    LABELS            "unit;docx;parser"
)

# ─── Optional: valgrind memory-check target ───────────────────────────────────
# Creates a second CTest entry that runs the binary under valgrind when it is
# available.  Skipped silently on systems without valgrind.
find_program(VALGRIND_EXECUTABLE valgrind)
if(VALGRIND_EXECUTABLE)
    add_test(
        NAME    DocxParser.MemCheck
        COMMAND ${VALGRIND_EXECUTABLE}
                    --error-exitcode=1
                    --leak-check=full
                    --track-origins=yes
                    --suppressions=${CMAKE_SOURCE_DIR}/tests/valgrind.supp
                $<TARGET_FILE:test_docx_parser>
    )
    set_tests_properties(DocxParser.MemCheck PROPERTIES
        WORKING_DIRECTORY "${CMAKE_SOURCE_DIR}"
        TIMEOUT           120
        LABELS            "memcheck;docx;parser"
    )
endif()
