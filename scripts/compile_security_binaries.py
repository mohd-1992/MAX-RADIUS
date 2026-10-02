# -*- coding: utf-8 -*-
"""
scripts/compile_security_binaries.py
-----------------------------------
Compiles security-critical licensing and anti-tamper modules into native
C-extensions (.so binaries on Linux / .pyd on Windows).

Target modules:
  - core/licensing.py -> core/licensing.*.so
  - core/hardware_fingerprint.py -> core/hardware_fingerprint.*.so
  - services/license_guard_service.py -> services/license_guard_service.*.so

Usage:
  # In container or production build environment:
  python3 scripts/compile_security_binaries.py [--remove-sources]
"""

import os
import sys
import glob
import shutil
import argparse
from setuptools import setup, Extension
from Cython.Build import cythonize

TARGET_FILES = [
    "core/licensing.py",
    "core/hardware_fingerprint.py",
    "services/license_guard_service.py"
]

def compile_binaries(remove_sources=False):
    # Dynamically locate project root
    if os.path.exists("core/licensing.py"):
        base_dir = os.path.abspath(".")
    elif os.path.exists("/app/core/licensing.py"):
        base_dir = "/app"
    else:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        parent_dir = os.path.abspath(os.path.join(script_dir, ".."))
        if os.path.exists(os.path.join(parent_dir, "core", "licensing.py")):
            base_dir = parent_dir
        else:
            base_dir = script_dir
    os.chdir(base_dir)

    print(f"🔒 [Security Binary Compiler] Working directory: {base_dir}")
    print(f"🔒 [Security Binary Compiler] Targets to compile: {', '.join(TARGET_FILES)}")

    extensions = []
    for rel_path in TARGET_FILES:
        full_path = os.path.join(base_dir, rel_path)
        if not os.path.exists(full_path):
            print(f"⚠️ Target file not found: {rel_path}, skipping.")
            continue
        mod_name = os.path.splitext(rel_path)[0].replace("/", ".").replace("\\", ".")
        extensions.append(Extension(mod_name, [rel_path]))

    if not extensions:
        print("❌ No extensions to compile.")
        return False

    # Run cythonize build
    build_dir = os.path.join(base_dir, "build_temp")
    try:
        setup(
            name="max_radius_security_core",
            ext_modules=cythonize(
                extensions,
                compiler_directives={
                    'language_level': "3",
                    'always_allow_keywords': True
                },
                build_dir=build_dir
            ),
            script_args=["build_ext", "--inplace"]
        )
        print("✅ Cython native compilation succeeded!")
    except Exception as e:
        print(f"❌ Cython compilation error: {e}")
        return False
    finally:
        # Cleanup temporary build dirs and .c intermediate files
        if os.path.exists(build_dir):
            shutil.rmtree(build_dir, ignore_errors=True)
        if os.path.exists(os.path.join(base_dir, "build")):
            shutil.rmtree(os.path.join(base_dir, "build"), ignore_errors=True)
        for c_file in glob.glob(os.path.join(base_dir, "core", "*.c")) + glob.glob(os.path.join(base_dir, "services", "*.c")):
            try:
                os.remove(c_file)
            except Exception:
                pass

    # Verify compiled .so / .pyd files exist
    all_compiled = True
    for rel_path in TARGET_FILES:
        dir_name = os.path.dirname(os.path.join(base_dir, rel_path))
        base_name = os.path.splitext(os.path.basename(rel_path))[0]
        pattern = os.path.join(dir_name, f"{base_name}*.so")
        pyd_pattern = os.path.join(dir_name, f"{base_name}*.pyd")
        matches = glob.glob(pattern) + glob.glob(pyd_pattern)
        if matches:
            print(f"  ✨ Found binary: {os.path.relpath(matches[0], base_dir)}")
        else:
            print(f"  ❌ Missing compiled binary for: {rel_path}")
            all_compiled = False

    if all_compiled and remove_sources:
        print("🧹 Removing plain text python sources for compiled modules...")
        for rel_path in TARGET_FILES:
            py_file = os.path.join(base_dir, rel_path)
            if os.path.exists(py_file):
                # Keep a backup .bak before deleting
                bak_file = py_file + ".bak"
                shutil.copy2(py_file, bak_file)
                os.remove(py_file)
                print(f"  🗑️ Removed {rel_path} (Backup kept at {rel_path}.bak)")

    return all_compiled

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Compile MAX RADIUS security modules into native binaries.")
    parser.add_argument("--remove-sources", action="store_true", help="Remove original .py source files after successful compilation (keeps .bak)")
    args = parser.parse_args()

    success = compile_binaries(remove_sources=args.remove_sources)
    if not success:
        sys.exit(1)
