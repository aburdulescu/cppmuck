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
        parent: str,
        namespace: [str],
        args: [Arg],
        return_type: str,
        file: str,
        line: int,
        has_return_type: bool,
    ):
        self.name = name
        self.parent = parent
        self.namespace = namespace
        self.args = args
        self.return_type = return_type
        self.file = file
        self.line = line
        self.has_return_type = has_return_type

        self.full_name = self.__full_name()

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

        if self.has_return_type:
            body = ""
            if self.return_type != "void":
                body = " return {}; "
            return "auto %s::%s(%s) -> %s {%s}" % (
                self.parent,
                self.name,
                args,
                self.return_type,
                body,
            )
        else:
            return "auto %s::%s(%s) {}" % (
                self.parent,
                self.name,
                args,
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


def parse_file(root_dir: str, build_dir: str, filepath: str, typename: str) -> []:
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

    all_funcs = []

    for c in tu.cursor.walk_preorder():
        if not str(c.location.file).startswith(root_dir):
            continue

        if c.kind not in kinds:
            continue

        if c.access_specifier != AccessSpecifier.PUBLIC:
            continue

        args = []
        for arg in c.get_arguments():
            args.append(
                Arg(
                    name=str(arg.spelling),
                    type=str(arg.type.spelling),
                )
            )

        fn = Func(
            name=c.spelling,
            parent=get_parent(c),
            namespace=get_namespace(c),
            args=args,
            return_type=str(c.result_type.spelling),
            file=str(c.location.file),
            line=int(c.location.line),
            has_return_type=(c.kind != CursorKind.CONSTRUCTOR),
        )

        if typename is not None:
            if not fn.full_name.startswith(typename):
                continue

        if fn not in all_funcs:
            print(
                "%s:%d %s %s"
                % (
                    os.path.relpath(fn.file, root_dir),
                    fn.line,
                    fn.namespace,
                    fn,
                )
            )
            all_funcs.append(fn)

    return all_funcs


def generate_file(all_funcs: [Func], filepath: str, output_file: str, header_ext: str):
    ns_to_funcs = {}
    for fn in all_funcs:
        if fn.namespace not in ns_to_funcs:
            ns_to_funcs[fn.namespace] = []
        ns_to_funcs[fn.namespace].append(fn)

    s = ""

    s += "// GENERATED FILE, DO NOT EDIT!\n"
    s += "// clang-format off\n"
    s += "// generated with: cppmuck %s\n" % (" ".join(sys.argv[1:]))
    s += "// clang-format on\n\n"

    s += '#include "%s"\n\n' % (
        os.path.splitext(os.path.basename(filepath))[0] + header_ext
    )

    for ns, funcs in ns_to_funcs.items():
        ns_list = ns.split("::")

        for v in ns_list:
            s += "namespace %s {\n" % (v)
        s += "\n"

        for fn in funcs:
            s += "%s\n\n" % (fn)

        for _ in ns_list:
            s += "}\n"

    out = "out.cpp"
    if output_file is not None:
        out = output_file

    with open(out, "w") as f:
        f.write(s)


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
        "--header-ext",
        default=".hpp",
        help="Extension of the header file",
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

    start = time.perf_counter_ns()
    all_funcs = parse_file(args.root_dir, args.build_dir, args.filepath, args.typename)
    end = time.perf_counter_ns()
    print("parse: %f s" % ((end - start) / 1e9))

    if args.typename is not None:
        start = time.perf_counter_ns()
        generate_file(all_funcs, args.filepath, args.output_file, args.header_ext)
        end = time.perf_counter_ns()
        print("generate: %f s" % ((end - start) / 1e9))


if __name__ == "__main__":
    main()
