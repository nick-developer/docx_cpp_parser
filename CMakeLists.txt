cmake_minimum_required(VERSION 3.15)
project(docx_comment_parser VERSION 1.0.0 LANGUAGES CXX)

# ─── Standard & optimisation flags ──────────────────────────────────────────
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()

# LTO for Release builds
include(CheckIPOSupported)
check_ipo_supported(RESULT _ipo_ok OUTPUT _ipo_err)
if(_ipo_ok)
  set(CMAKE_INTERPROCEDURAL_OPTIMIZATION_RELEASE ON)
endif()

# ─── Dependencies ────────────────────────────────────────────────────────────
# libxml2 not required - using built-in XML parser
find_package(ZLIB    REQUIRED)

# ─── Core shared library ─────────────────────────────────────────────────────
add_library(docx_comment_parser SHARED
    src/docx_parser.cpp
    src/batch_parser.cpp
    src/zip_reader.cpp
    src/xml_parser.cpp
)

target_include_directories(docx_comment_parser
    PUBLIC
        $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
        $<INSTALL_INTERFACE:include>
    PRIVATE
)

target_link_libraries(docx_comment_parser
    PRIVATE
        ZLIB::ZLIB
)

# Hide all symbols except those explicitly exported with DOCX_API
set_target_properties(docx_comment_parser PROPERTIES
    CXX_VISIBILITY_PRESET hidden
    VISIBILITY_INLINES_HIDDEN ON
    VERSION   ${PROJECT_VERSION}
    SOVERSION 1
)

target_compile_options(docx_comment_parser PRIVATE
    $<$<CXX_COMPILER_ID:GNU,Clang>:-Wall -Wextra -Wpedantic>
    $<$<CONFIG:Release>:-O3 -DNDEBUG>
)

# ─── Python extension (optional) ─────────────────────────────────────────────
option(BUILD_PYTHON_BINDINGS "Build Python bindings via pybind11" ON)

if(BUILD_PYTHON_BINDINGS)
    find_package(Python3 REQUIRED COMPONENTS Interpreter Development)
    find_package(pybind11 CONFIG QUIET)

    if(NOT pybind11_FOUND)
        # Try to locate pybind11 via pip-installed package
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

        target_include_directories(_docx_comment_parser PRIVATE
            ${CMAKE_CURRENT_SOURCE_DIR}/include
            
        )

        target_link_libraries(_docx_comment_parser PRIVATE
            docx_comment_parser
            
            ZLIB::ZLIB
        )

        # Install alongside the Python package
        install(TARGETS _docx_comment_parser
            LIBRARY DESTINATION ${Python3_SITEARCH}
        )
    else()
        message(WARNING "pybind11 not found – Python bindings will not be built. "
                        "Install with: pip install pybind11")
    endif()
endif()

# ─── Install rules ────────────────────────────────────────────────────────────
include(GNUInstallDirs)

install(TARGETS docx_comment_parser
    EXPORT  docx_comment_parserTargets
    LIBRARY DESTINATION ${CMAKE_INSTALL_LIBDIR}
    ARCHIVE DESTINATION ${CMAKE_INSTALL_LIBDIR}
    RUNTIME DESTINATION ${CMAKE_INSTALL_BINDIR}
    INCLUDES DESTINATION ${CMAKE_INSTALL_INCLUDEDIR}
)

install(FILES
    include/docx_comment_parser.h
    include/zip_reader.h
    DESTINATION ${CMAKE_INSTALL_INCLUDEDIR}/docx_comment_parser
)

install(EXPORT docx_comment_parserTargets
    FILE        docx_comment_parserTargets.cmake
    NAMESPACE   docx::
    DESTINATION ${CMAKE_INSTALL_LIBDIR}/cmake/docx_comment_parser
)

# ─── Tests ────────────────────────────────────────────────────────────────────
option(BUILD_TESTS "Build test suite" ON)

if(BUILD_TESTS)
    enable_testing()
    add_subdirectory(tests)
endif()
