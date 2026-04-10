# =============================================================================
# tests/CMakeLists.txt
# =============================================================================

cmake_minimum_required(VERSION 3.15)

# ─── zlib ────────────────────────────────────────────────────────────────────
# On MSVC: crc32 body is compiled into the test TU via VENDOR_ZLIB_IMPLEMENTATION;
#          no library link needed.
# On Linux / macOS / MinGW: link the system libz.
if(NOT MSVC)
  find_package(ZLIB REQUIRED)
endif()

# ─── Test executable ──────────────────────────────────────────────────────────
add_executable(test_docx_parser
    test_docx_parser.cpp
)

set_target_properties(test_docx_parser PROPERTIES
    CXX_STANDARD          17
    CXX_STANDARD_REQUIRED ON
    CXX_EXTENSIONS        OFF
)

target_include_directories(test_docx_parser
    PRIVATE
        ${CMAKE_SOURCE_DIR}/include
        $<$<CXX_COMPILER_ID:MSVC>:${CMAKE_SOURCE_DIR}/vendor>
)

target_link_libraries(test_docx_parser
    PRIVATE
        docx_comment_parser
        $<$<NOT:$<CXX_COMPILER_ID:MSVC>>:ZLIB::ZLIB>
)

target_compile_options(test_docx_parser PRIVATE
    $<$<CXX_COMPILER_ID:GNU,Clang>:-Wall -Wextra -Wpedantic>
    $<$<AND:$<CONFIG:Debug>,$<CXX_COMPILER_ID:GNU,Clang>>:-g -O0>
    $<$<AND:$<CONFIG:Release>,$<CXX_COMPILER_ID:GNU,Clang>>:-O2 -DNDEBUG>
)

# ─── Windows DLL placement ────────────────────────────────────────────────────
# The runtime loader searches the exe's own directory first.  Copy our DLL
# there so the test runs without any extra PATH setup.
if(WIN32)
    add_custom_command(TARGET test_docx_parser POST_BUILD
        COMMAND ${CMAKE_COMMAND} -E copy_if_different
            $<TARGET_FILE:docx_comment_parser>
            $<TARGET_FILE_DIR:test_docx_parser>
        COMMENT "Copying docx_comment_parser DLL next to test executable"
        VERBATIM
    )

    # On MinGW: also copy zlib1.dll (runtime dependency of our DLL) and
    # pre-resolve the compiler bin dir for use in ENVIRONMENT_MODIFICATION below.
    if(MINGW)
        get_filename_component(_mingw_bin "${CMAKE_CXX_COMPILER}" DIRECTORY)

        find_file(_zlib1_dll
            NAMES zlib1.dll
            HINTS "${_mingw_bin}"
            NO_DEFAULT_PATH
        )
        if(_zlib1_dll)
            add_custom_command(TARGET test_docx_parser POST_BUILD
                COMMAND ${CMAKE_COMMAND} -E copy_if_different
                    "${_zlib1_dll}"
                    $<TARGET_FILE_DIR:test_docx_parser>
                COMMENT "Copying zlib1.dll next to test executable"
                VERBATIM
            )
        endif()
    endif()
endif()

# ─── CTest registration ───────────────────────────────────────────────────────
add_test(
    NAME    DocxParser.BasicParsing
    COMMAND test_docx_parser
)

set_tests_properties(DocxParser.BasicParsing PROPERTIES
    WORKING_DIRECTORY "${CMAKE_SOURCE_DIR}"
    TIMEOUT           30
    LABELS            "unit;docx;parser"
)

# Prepend the MinGW compiler bin dir to PATH so CTest finds libgcc_s_seh-1.dll,
# libstdc++-6.dll, libwinpthread-1.dll even when MSYS2 is not on the system PATH.
# Must be set AFTER add_test().
# ENVIRONMENT_MODIFICATION (CMake 3.22+) correctly handles the OS path separator.
if(WIN32 AND MINGW)
    if(CMAKE_VERSION VERSION_GREATER_EQUAL "3.22")
        set_tests_properties(DocxParser.BasicParsing PROPERTIES
            ENVIRONMENT_MODIFICATION
                "PATH=path_list_prepend:${_mingw_bin}"
        )
    endif()
    # For CMake < 3.22 the DLL copies above are the fallback: zlib1.dll is
    # copied next to the exe; libgcc / libstdc++ / libwinpthread are assumed
    # to be reachable through the existing system PATH (standard MSYS2 setup).
endif()

# ─── Optional: valgrind memory-check target ───────────────────────────────────
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
