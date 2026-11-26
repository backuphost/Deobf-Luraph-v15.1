#!/usr/bin/env python3
"""Build the Luau interpreter every engine of this repository runs on.

    python3 build_luau.py            # clone Luau 0.739, patch, build, install into bin/ and deobf/bin/
    python3 build_luau.py --check    # only report what is installed
    python3 build_luau.py --src DIR  # use an existing Luau checkout (it gets patched in place)

Protected scripts measure their interpreter (C stack use of pcall, what a C function looks like in
debug.info, error positions, the vector type's metatable), so a stock or newer Luau gives wrong
decryption keys.  The build is Luau 0.739 with two patches:

  * devm/luau_cli.patch: readfile/writefile/ewrite, cwrap (Lua functions that look like C functions
    to the script), resumetop, setvectormetatable, running a returned function as the main function
    of a fresh thread, and the natives behind the executor API of the mock (__LPH_NATIVE:
    getrawmetatable, setrawmetatable, setreadonly, isreadonly, clonefunction).  Used by
    devm/harness.lua; deobf/envlog.luau uses resumetop.
  * the vector type's metatable is left writable (stock Luau freezes it), so the mock environment
    can give Vector3 values Roblox's members.  Used by deobf/envlog.luau and devm.

Needs git, cmake and a C++ compiler (g++ or clang++; MSVC from a developer prompt).  Takes a few
minutes.  Nothing is downloaded except the Luau source from github.com/luau-lang/luau.
"""
import argparse
import os
import platform
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = "https://github.com/luau-lang/luau.git"
TAG = "0.739"
EXE = ".exe" if os.name == "nt" else ""
INSTALL_DIRS = [os.path.join(HERE, "bin"), os.path.join(HERE, "deobf", "bin")]
CLI_PATCH = os.path.join(HERE, "devm", "luau_cli.patch")

# deobf/build_luau.py applies the same edit; kept in step with it
VEC_FILE = os.path.join("VM", "src", "lveclib.cpp")
VEC_FREEZE = "    lua_setreadonly(L, -1, true);\n    lua_pop(L, 1); // pop the metatable\n"
VEC_PATCHED = ("    // deobf: left writable, envlog.luau adds Roblox's Vector3 members\n"
               "    lua_pop(L, 1); // pop the metatable\n")

SMOKE = ('local v = vector.create(1, 2, 3)\n'
         'local mt = getmetatable(v)\n'
         'local writable = mt ~= nil and pcall(function() mt.__devm_probe = true; mt.__devm_probe = nil end)\n'
         'print(cwrap ~= nil, readfile ~= nil, writefile ~= nil, resumetop ~= nil, __LPH_RUNMAIN ~= nil,\n'
         '      type(__LPH_NATIVE) == "table" and __LPH_NATIVE.getrawmetatable ~= nil, writable)\n')
SMOKE_NAMES = ["cwrap", "readfile", "writefile", "resumetop", "run-returned-function", "executor natives",
               "writable vector metatable"]


def say(msg):
    print("[build_luau] " + msg, file=sys.stderr, flush=True)


def run(cmd, cwd=None):
    say(" ".join(cmd))
    subprocess.run(cmd, cwd=cwd, check=True)


def smoke(exe):
    """[(feature, present)] for an installed interpreter, or None if it does not run."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "smoke.luau")
        with open(path, "w") as f:
            f.write(SMOKE)
        try:
            r = subprocess.run([exe, path], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return None
    words = r.stdout.split()
    if r.returncode != 0 or len(words) != len(SMOKE_NAMES):
        return None
    return list(zip(SMOKE_NAMES, [w == "true" for w in words]))


def shown(path):
    rel = os.path.relpath(path, HERE)
    return path if rel.startswith("..") else rel


def check():
    """Report the installed interpreters; True when every engine can run."""
    ok = True
    for d in INSTALL_DIRS:
        for name in ("luau", "luau-ast"):
            path = os.path.join(d, name + EXE)
            if not os.path.isfile(path):
                say("missing  " + shown(path))
                ok = False
                continue
            if name == "luau":
                feats = smoke(path)
                if feats is None:
                    say("BROKEN   %s (does not run on this machine: %s)" % (shown(path), platform.machine()))
                    ok = False
                    continue
                lacking = [n for n, have in feats if not have]
                if lacking:
                    say("UNPATCHED %s (lacks: %s)" % (shown(path), ", ".join(lacking)))
                    ok = False
                    continue
            say("ok       " + shown(path))
    return ok


def patch(src):
    # 1. the CLI patch (a git diff); already applied when it reverses cleanly
    rev = subprocess.run(["git", "apply", "--reverse", "--check", CLI_PATCH], cwd=src, capture_output=True)
    if rev.returncode != 0:
        fwd = subprocess.run(["git", "apply", "--check", CLI_PATCH], cwd=src, capture_output=True, text=True)
        if fwd.returncode != 0:
            sys.exit("[build_luau] devm/luau_cli.patch does not apply to %s (is it Luau %s?):\n%s" % (src, TAG, fwd.stderr))
        run(["git", "apply", CLI_PATCH], cwd=src)
    # 2. writable vector metatable
    path = os.path.join(src, VEC_FILE)
    with open(path, encoding="utf-8") as f:
        text = f.read()
    if VEC_PATCHED not in text:
        if text.count(VEC_FREEZE) != 1:
            sys.exit("[build_luau] %s is not what Luau %s has: patch createmetatable() by hand" % (VEC_FILE, TAG))
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text.replace(VEC_FREEZE, VEC_PATCHED))


def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--check", action="store_true", help="only report what is installed")
    ap.add_argument("--src", help="existing Luau %s checkout to patch and build (default: clone into .build/luau/src)" % TAG)
    ap.add_argument("--jobs", type=int, default=os.cpu_count() or 2)
    ap.add_argument("--install", action="append", metavar="DIR",
                    help="install into DIR instead of bin/ and deobf/bin/ (may be repeated)")
    args = ap.parse_args()
    if args.install:
        INSTALL_DIRS[:] = [os.path.abspath(d) for d in args.install]
    if args.check:
        sys.exit(0 if check() else 1)
    for tool in ("git", "cmake"):
        if not shutil.which(tool):
            sys.exit("[build_luau] %s not found: install git, cmake and a C++ compiler first (see setup.sh)" % tool)
    work = os.path.join(HERE, ".build", "luau")
    src = args.src or os.path.join(work, "src")
    if not os.path.isdir(os.path.join(src, "VM")):
        os.makedirs(os.path.dirname(src), exist_ok=True)
        run(["git", "clone", "-q", "--depth", "1", "--branch", TAG, REPO, src])
    patch(src)
    build = os.path.join(work, "build") if not args.src else os.path.join(src, "build-luraph")
    cfg = ["cmake", "-S", src, "-B", build, "-DCMAKE_BUILD_TYPE=Release", "-DLUAU_BUILD_TESTS=OFF"]
    if shutil.which("ninja") and not os.path.exists(os.path.join(build, "CMakeCache.txt")):
        cfg += ["-G", "Ninja"]
    if os.name != "nt":
        # plain -O2: the same code on every machine (no -march=native), so a build can be copied
        cfg += ["-DCMAKE_C_FLAGS_RELEASE=-O2 -DNDEBUG", "-DCMAKE_CXX_FLAGS_RELEASE=-O2 -DNDEBUG"]
    run(cfg)
    run(["cmake", "--build", build, "--config", "Release", "--target", "Luau.Repl.CLI", "Luau.Ast.CLI",
         "--parallel", str(args.jobs)])
    for name in ("luau", "luau-ast"):
        built = next((p for p in (os.path.join(build, name + EXE), os.path.join(build, "Release", name + EXE))
                      if os.path.isfile(p)), None)
        if built is None:
            sys.exit("[build_luau] %s was not built under %s" % (name + EXE, build))
        for d in INSTALL_DIRS:
            os.makedirs(d, exist_ok=True)
            shutil.copy2(built, os.path.join(d, name + EXE))
    if not check():
        sys.exit("[build_luau] the build finished but the installed interpreter failed its check")
    say("done: bin/luau%s and bin/luau-ast%s are ready" % (EXE, EXE))


if __name__ == "__main__":
    main()
