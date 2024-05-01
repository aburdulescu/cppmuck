#!/usr/bin/env python3

from clang.cindex import (
    Index,
    CursorKind,
    CompilationDatabase,
    CompilationDatabaseError,
    TranslationUnitLoadError,
    Diagnostic,
    AccessSpecifier,
    Cursor,
    ExceptionSpecificationKind,
)

import argparse
import os
import sys
import subprocess
import time


def get_parent(cursor) -> str:
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

    return "::".join(result)


def get_namespace(cursor) -> str:
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

    return "::".join(result)


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


def argv_from_compdb(directory, arguments) -> list[str]:
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


class Arg(object):
    def __init__(self, c: Cursor):
        self.name = str(c.spelling)
        self.type = str(c.type.spelling)

    def __eq__(self, other):
        if not isinstance(other, Arg):
            return NotImplemented
        return self.name == other.name and self.type == other.type

    def __repr__(self):
        return '{type="%s", name="%s"}' % (self.type, self.name)


class Func(object):
    def __init__(self, c: Cursor):
        self.name = c.spelling
        self.parent = get_parent(c)
        self.namespace = get_namespace(c)
        self.return_type = str(c.result_type.spelling)
        self.file = str(c.location.file)
        self.line = int(c.location.line)
        self.is_ctor = (
            c.kind == CursorKind.CONSTRUCTOR or c.kind == CursorKind.DESTRUCTOR
        )
        self.is_const = c.is_const_method()

        self.except_kind = ""
        if c.exception_specification_kind == ExceptionSpecificationKind.BASIC_NOEXCEPT:
            self.except_kind = "noexcept"

        self.args = []
        for arg in c.get_arguments():
            self.args.append(Arg(arg))

        self.full_name = self.__full_name()

    def __eq__(self, other):
        if not isinstance(other, Func):
            return NotImplemented
        if self.name != other.name:
            return False
        if self.parent != other.parent:
            return False
        if self.namespace != other.namespace:
            return False
        if self.return_type != other.return_type:
            return False
        if len(self.args) != len(other.args):
            return False
        for arg in self.args:
            if arg not in other.args:
                return False
        return True

    def __str__(self):
        args = ""
        for arg in self.args:
            args += f"{arg.type} {arg.name}, "
        if args != "":
            args = args[: len(args) - 2]

        if self.is_ctor:
            return "%s::%s(%s) %s {}" % (
                self.parent,
                self.name,
                args,
                self.except_kind,
            )
        else:
            body = ""
            if self.return_type != "void":
                body = " return {}; "
            extras = ""
            if self.is_const:
                extras += " const"
            if self.except_kind != "":
                extras += " " + self.except_kind
            return "%s %s::%s(%s) %s {%s}" % (
                self.return_type,
                self.parent,
                self.name,
                args,
                extras,
                body,
            )

    def __full_name(self):
        result = ""
        if self.namespace != "":
            result += self.namespace
        if self.parent != "":
            if result != "":
                result += "::"
            result += self.parent
        if result != "":
            result += "::"
        result += self.name
        return result


def parse_file(
    root_dir: str,
    build_dir: str,
    filepath: str,
    typenames: list[str],
) -> []:
    try:
        comp_db = CompilationDatabase.fromDirectory(build_dir)
    except CompilationDatabaseError:
        print('error loading compilation database from "%s"' % build_dir)
        sys.exit(1)

    index = Index.create()

    compile_command = None
    for v in comp_db.getAllCompileCommands():
        filename = os.path.join(v.directory, v.filename)
        if filename == filepath:
            compile_command = v

    if compile_command is None:
        print("error: cannot find the file in the compilation database")
        sys.exit(1)

    argv = argv_from_compdb(compile_command.directory, compile_command.arguments)

    try:
        tu = index.parse(None, args=argv[1:])
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
        CursorKind.DESTRUCTOR,
        CursorKind.CXX_METHOD,
        CursorKind.FUNCTION_DECL,
        CursorKind.FUNCTION_TEMPLATE,
    ]

    all_funcs = []

    for c in tu.cursor.walk_preorder():
        if not str(c.location.file).startswith(root_dir):
            continue
        if c.kind not in kinds:
            continue
        if c.access_specifier != AccessSpecifier.PUBLIC:
            continue
        if c.is_default_method():
            continue
        if c.is_deleted_method():
            continue
        if not c.is_definition():
            continue

        fn = Func(c)

        if typenames:
            found = False
            for tn in typenames:
                if fn.full_name.startswith(tn):
                    found = True
                    break
            if not found:
                continue

        if fn not in all_funcs:
            # print(
            #     "%s:%d %s %s"
            #     % (
            #         os.path.relpath(fn.file, root_dir),
            #         fn.line,
            #         fn.namespace,
            #         fn,
            #     )
            # )
            # print(get_func_body(c))
            all_funcs.append(fn)
        else:
            dup = None
            for v in all_funcs:
                if v == fn:
                    dup = v
            print(f"warning: function '{fn}' is a duplicate of '{dup}'")

    return all_funcs


def get_func_body(c: Cursor) -> str:
    tokens = [tok for tok in c.get_tokens()]
    print([tok.spelling for tok in tokens])

    start = None
    for tok in tokens:
        if tok.spelling == "{":
            start = tok.location
            break
    if start is None:
        return ""

    end = None
    for tok in reversed(tokens):
        if tok.spelling == "}":
            end = tok.location
            break
    if end is None:
        return ""

    s = ""
    with open(c.location.file.name, "r") as f:
        lines = f.readlines()
        s += lines[start.line - 1][start.column - 1 :]
        for i in range(start.line, end.line - 1):
            s += lines[i]
        s += lines[end.line - 1][: end.column]

    return s


def generate_file(all_funcs: [Func], filepath: str, output_file: str):
    ns_to_funcs = {}
    for fn in all_funcs:
        if fn.namespace not in ns_to_funcs:
            ns_to_funcs[fn.namespace] = []
        ns_to_funcs[fn.namespace].append(fn)

    s = ""

    filename = os.path.basename(filepath)
    s += '#include "%s.hpp"\n\n' % (os.path.splitext(filename)[0])

    for ns, funcs in ns_to_funcs.items():
        ns_list = ns.split("::")

        for v in ns_list:
            s += "namespace %s {\n" % (v)
        s += "\n"

        for fn in funcs:
            s += "%s\n\n" % (fn)

        for _ in ns_list:
            s += "}\n"

        s += "\n"

    with open(output_file, "w") as f:
        f.write(s)


def main():
    parser = argparse.ArgumentParser(
        description="Generate C++ muck(mocks/stubs) needed for tests.",
    )
    parser.add_argument(
        "-r",
        "--root-dir",
        help="Project root path, default is current directory",
        type=str,
        default=".",
    )
    parser.add_argument(
        "-b",
        "--build-dir",
        help="Path to the directory where the compilation database is stored, relative to -r, default is b/",
        type=str,
        default="b",
    )
    parser.add_argument(
        "-o",
        "--output-file",
        help="Path to output file, default is cppmuck.cpp",
        type=str,
        default="cppmuck.cpp",
    )
    parser.add_argument(
        "filepath",
        help="Path to the source file which contains the type you want, relative to -r",
        type=str,
    )
    parser.add_argument(
        "typenames",
        metavar="typename",
        help="Name of the type you want",
        type=str,
        nargs="*",
    )

    args = parser.parse_args()

    args.root_dir = os.path.abspath(args.root_dir)
    args.build_dir = os.path.abspath(os.path.join(args.root_dir, args.build_dir))
    args.filepath = os.path.abspath(os.path.join(args.root_dir, args.filepath))
    args.output_file = os.path.abspath(args.output_file)

    start = time.perf_counter_ns()
    all_funcs = parse_file(args.root_dir, args.build_dir, args.filepath, args.typenames)
    end = time.perf_counter_ns()
    print("parse: %f s" % ((end - start) / 1e9))

    start = time.perf_counter_ns()
    generate_file(all_funcs, args.filepath, args.output_file)
    end = time.perf_counter_ns()
    print("generate: %f s" % ((end - start) / 1e9))


if __name__ == "__main__":
    main()
