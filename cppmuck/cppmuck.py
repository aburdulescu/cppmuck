#!/usr/bin/env python3

from clang.cindex import (
    Index,
    CursorKind,
    CompilationDatabase,
    CompilationDatabaseError,
    TranslationUnit,
    TranslationUnitLoadError,
    Diagnostic,
    AccessSpecifier,
)

import argparse
import os
import sys
import subprocess


def get_parents(cursor) -> []:
    result = []

    kinds = [
        CursorKind.CLASS_DECL,
        CursorKind.CLASS_TEMPLATE,
        CursorKind.STRUCT_DECL,
    ]

    t = cursor.semantic_parent
    while True:
        if t.kind not in kinds:
            break
        result.insert(0, str(t.spelling))
        t = t.semantic_parent

    return result


def get_namespaces(cursor) -> []:
    result = []

    kinds = [
        CursorKind.CLASS_DECL,
        CursorKind.CLASS_TEMPLATE,
        CursorKind.STRUCT_DECL,
    ]

    t = cursor.semantic_parent
    while True:
        if t.kind in kinds:
            t = t.semantic_parent
            continue
        if t.kind != CursorKind.NAMESPACE:
            break
        result.insert(0, str(t.spelling))
        t = t.semantic_parent

    return result


# parse the output of `cc -E -v -x c++ /dev/null` to get system include paths
def args_from_driver_output(output):
    r = []
    inc_list_started = False
    for line in output.splitlines():
        line = line.strip(" ").strip("\t")
        if line == "":
            continue
        if line.startswith("Target"):
            target = line.split(":")[1]
            r.append("--target=" + target.strip(" "))
        if line == "#include <...> search starts here:":
            inc_list_started = True
            continue
        if line == "End of search list.":
            inc_list_started = False
            continue
        if inc_list_started:
            r.append("-isystem")
            r.append(line)
    return r


def argv_from_compdb(directory, arguments) -> [str]:
    argv = []
    for a in arguments:
        if a == "-fno-aggressive-loop-optimizations":
            continue
        if a == "-Werror":
            continue
        if a.startswith("-I"):
            # make relative -I into absolute
            ipath = a[2:]
            if not os.path.isabs(ipath):
                ipath = os.path.normpath(os.path.join(directory, ipath))
                argv.append("-I" + ipath)
            else:
                argv.append(a)
        elif a.startswith("-o"):
            # -w inhibits all warning messages
            argv.append("-w")
            argv.append("-ferror-limit=0")
            argv.append(a)
        else:
            argv.append(a)

    if len(argv) == 0:
        raise RuntimeError("argv is empty")

    driver = argv[0]

    result = subprocess.run(
        [driver, "-E", "-v", "-x", "c++", "/dev/null"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    if result.returncode != 0:
        print("returncode:", result.returncode)
        print("stdout:", result.stdout)
        print("stderr:", result.stderr)
        raise RuntimeError("failed to run driver")

    argv.extend(args_from_driver_output(result.stdout))

    return argv


class Arg:
    def __init__(self, name: str, type: str):
        self.name = name
        self.type = type

    def __eq__(self, other):
        if not isinstance(other, Arg):
            return NotImplemented
        return self.name == other.name and self.type == other.type


class Func:
    def __init__(
        self,
        name: str,
        parents: [str],
        namespaces: [str],
        args: [Arg],
        return_type: str,
        file: str,
        line: int,
    ):
        self.name = name
        self.parents = parents
        self.namespaces = namespaces
        self.args = args
        self.return_type = return_type
        self.file = file
        self.line = line

    def __eq__(self, other):
        if not isinstance(other, Func):
            return NotImplemented
        return (
            self.name == other.name
            and self.args == self.args
            and self.return_type == other.return_type
        )

    def __str__(self):
        args = ""
        for arg in self.args:
            args += f"{arg.type} {arg.name}, "
        if args != "":
            args = args[: len(args) - 2]
        body = ""
        if self.return_type != "void":
            body = " return {}; "
        return "auto %s(%s) -> %s {%s}" % (self.name, args, self.return_type, body)

    def full_name(self):
        full_name = ""
        if len(self.namespaces) != 0:
            full_name += "::".join(self.namespaces)
        if len(self.parents) != 0:
            if full_name != "":
                full_name += "::"
            full_name += "::".join(self.parents)
        if full_name != "":
            full_name += "::"
        full_name += self.name
        return full_name


def is_in_src_paths(src_paths: [str], filename: str) -> bool:
    for src_path in src_paths:
        if filename.startswith(src_path):
            return True
    return False


def main():
    parser = argparse.ArgumentParser(
        description="Generate C++ muck(mocks/stubs) needed for tests.",
    )
    parser.add_argument(
        "-r",
        "--root-dir",
        help="Project root path",
        required=True,
    )
    parser.add_argument(
        "-b",
        "--build-dir",
        help="Path to the directory where the compilation database is stored, relative to -r",
        type=str,
        required=True,
    )
    parser.add_argument(
        "-o",
        "--output-file",
        help="Path to output file",
    )
    parser.add_argument(
        "filepath",
        help="Path to the source file which contains the type you want, relative to -r",
        type=str,
    )
    parser.add_argument(
        "typename", help="Name of the type you want", type=str, nargs="?"
    )

    args = parser.parse_args()

    args.root_dir = os.path.abspath(args.root_dir)
    args.build_dir = os.path.abspath(os.path.join(args.root_dir, args.build_dir))
    args.filepath = os.path.abspath(os.path.join(args.root_dir, args.filepath))

    try:
        comp_db = CompilationDatabase.fromDirectory(args.build_dir)
    except CompilationDatabaseError:
        print('error loading compilation database from "%s"' % args.build_dir)
        sys.exit(1)

    index = Index.create()

    compile_command = None
    for v in comp_db.getAllCompileCommands():
        filename = os.path.join(v.directory, v.filename)
        if filename == args.filepath:
            compile_command = v

    if compile_command is None:
        print("error: cannot find the file in the compilation database")
        sys.exit(1)

    funcs = []

    argv = argv_from_compdb(compile_command.directory, compile_command.arguments)

    parse_options = TranslationUnit.PARSE_SKIP_FUNCTION_BODIES

    try:
        tu = index.parse(
            None,
            args=argv[1:],
            options=parse_options,
        )
    except TranslationUnitLoadError:
        print("\nerror parsing translation unit")
        sys.exit(1)

    should_exit = False
    for diag in tu.diagnostics:
        print(f"\n{diag}")
        if diag.severity == Diagnostic.Fatal or diag.severity == Diagnostic.Error:
            should_exit = True
    if should_exit:
        sys.exit(1)

    kinds = [
        CursorKind.CONSTRUCTOR,
        CursorKind.CXX_METHOD,
        CursorKind.FUNCTION_DECL,
        CursorKind.FUNCTION_TEMPLATE,
    ]

    for c in tu.cursor.walk_preorder():
        if not str(c.location.file).startswith(args.root_dir):
            continue

        if c.kind not in kinds:
            continue

        if c.access_specifier != AccessSpecifier.PUBLIC:
            continue

        name = c.spelling
        parents = get_parents(c)
        namespaces = get_namespaces(c)

        fn_args = []
        for arg in c.get_arguments():
            fn_args.append(
                Arg(
                    name=str(arg.spelling),
                    type=str(arg.type.spelling),
                )
            )

        fn = Func(
            name=name,
            parents=parents,
            namespaces=namespaces,
            args=fn_args,
            return_type=str(c.result_type.spelling),
            file=str(c.location.file),
            line=int(c.location.line),
        )

        full_name = fn.full_name()
        if args.typename is not None:
            if not full_name.startswith(args.typename):
                continue

        if fn not in funcs:
            print(
                "%s:%d %s %s %s %s"
                % (
                    os.path.relpath(fn.file, args.root_dir),
                    fn.line,
                    fn.namespaces,
                    fn.parents,
                    full_name,
                    fn,
                )
            )
            funcs.append(fn)


if __name__ == "__main__":
    main()
