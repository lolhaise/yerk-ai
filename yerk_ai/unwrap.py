"""Which simple commands a shell string would run, and where a delete points.

The gate reads a command as it is written, so `timeout 5 rm x` looks like
`timeout` and `sh -c "rm x"` looks like `sh`. This module reads the same text
the way a shell would. It splits the text into the simple commands that would
run, peels the wrappers that only change how a command runs, reads again the
strings that `sh -c`, `eval`, `$(...)` and backquotes run, and resolves the
paths a delete would touch. It runs nothing and decides nothing: the gate keeps
the decisions, and a None from here means the text cannot be read for certain,
so the gate holds it.
"""

from __future__ import annotations

import itertools
import os
import re
from contextvars import ContextVar

# One argument with its quotes removed, and whether the shell would still
# expand part of it ($x, `x`, $(x)) before the program sees it.
_Word = tuple[str, bool]

_SEPARATOR = re.compile(r"\|\||&&|;;&?|;&|\|&|[;&|()\n]")
_REDIRECT = re.compile(r"\d*(?:&>>|<<<|<<-|<<|>>|<&|>&|&>|<>|>\||<|>)")
_ASSIGNMENT = re.compile(r"[A-Za-z_]\w*(?:\[[^]]*\])?\+?=")
_UNRESOLVED = re.compile(r"[$`*?\[{]|(?:^|/)~")
_BREAKS = " \t\n;&|()<>"
_KEYWORDS = {"!", "{", "}", "if", "then", "else", "elif", "fi", "do", "done", "while", "until", "esac"}
_SHELLS = {"sh", "bash", "zsh", "dash", "ksh"}
_DELETERS = {"rm", "rmdir", "unlink", "shred", "trash"}
# Each wrapper's short options that take a value, and its words before the command.
_WRAPPERS = {
    "timeout": ("ks", 1), "nice": ("n", 0), "env": ("uP", 0), "xargs": ("adEILnPs", 0),
    "command": ("", 0), "builtin": ("", 0), "sudo": ("CghpRrTtUu", 0), "doas": ("Cu", 0),
    "exec": ("a", 0), "nohup": ("", 0), "time": ("fo", 0), "setsid": ("", 0),
}
_LONG_VALUES = {"--signal", "--kill-after", "--adjustment", "--unset", "--user", "--group", "--prompt",
                "--arg-file", "--delimiter", "--max-args", "--max-lines", "--max-procs", "--max-chars"}
# Options that run the command in another folder or build it from a string. Once
# the wrapper is peeled the gate would no longer see them, so the text is held.
_HIDING = {("env", "C"), ("env", "S"), ("sudo", "D"), ("sudo", "i")}


class _Unreadable(Exception):
    """The text cannot be read for certain: an open quote, too deep, or a command known only at run time."""


# One read's own setting (a thread's, a task's): whether a command behind xargs keeps the words written after it.
_WHOLE: ContextVar[bool] = ContextVar("yerk_unwrap_whole", default=False)


def unwrap(command: str, depth: int = 4, whole: bool = False) -> list[list[str]] | None:
    """The simple commands the text would run, each as argv, with wrappers peeled.

    The strings that `sh -c`, `bash -c`, `zsh -c`, `eval`, `$(...)` and
    backquotes run are read again by the same rules, up to depth levels. A
    heredoc body is data. Behind xargs a command keeps only its program, since
    its arguments arrive at run time; with `whole` it keeps the words written
    after it too, for a reader that asks what a command starts with (the
    approval rules), never where a delete points. None when the text cannot be
    read: an open quote, more than depth levels, or a command known only at run time.
    """
    token = _WHOLE.set(whole)
    try:
        return _parse(command, depth)
    except _Unreadable:
        return None
    finally:
        _WHOLE.reset(token)


def delete_targets(argv: list[str], cwd: str) -> list[str] | None:
    """Every path a delete would remove, resolved against cwd and through symlinks.

    The deletes are rm, rmdir, unlink, shred and trash; find with -delete (its
    start paths); git rm (its files, unless --cached) and git clean that is not
    a dry run (cwd, since a repository can drop the need for -f in its config);
    rsync with --delete (its destination) and zip -m (its files). An empty list
    when none of these is the command: that is not a proof it deletes nothing
    (an interpreter, a wrapper this module does not peel, or a program not
    listed here can still delete), so the gate reads an empty list as unknown,
    never as safe. None when a target holds a variable, a glob or a `~`, is
    empty, or when a delete names no path at all, as behind xargs; and for tar
    --remove-files, whose members depend on its mode.
    """
    name, args = (os.path.basename(argv[0]), argv[1:]) if argv else ("", [])
    if name == "find" and "-delete" in args:
        if "-L" in args or "-follow" in args:
            return None  # it would follow links out of its start paths
        k = 0
        while k < len(args) and (args[k] in ("-H", "-P", "-D") or args[k].startswith("-O")):
            k += 2 if args[k] == "-D" else 1
        targets = list(itertools.takewhile(lambda a: a[:1] not in ("-", "(", "!", ")"), args[k:])) or ["."]
    elif name == "git":
        k, places = 0, ["."]
        while k < len(args) - 1 and args[k].startswith("-"):
            if args[k] == "-C":
                places.append(args[k + 1])
            k += 2 if args[k] in ("-C", "-c") else 1
        sub, rest = (args[k], args[k + 1:]) if k < len(args) else ("", [])
        dry = any(a == "--dry-run" or (a[:1] == "-" and a[1:2] != "-" and "n" in a) for a in rest)
        if sub not in ("clean", "rm") or dry or (sub == "rm" and "--cached" in rest):
            return []  # rm --cached drops the index entry and keeps the file
        if any(a.startswith(("--work-tree", "--git-dir")) for a in args[:k]):
            return None
        cut = rest.index("--") if "--" in rest else len(rest)
        named = [a for a in rest[:cut] if a[:1] != "-"] + rest[cut + 1:]
        if sub == "rm" and not named:
            return None
        targets = [os.path.join(*places, a) for a in named] if sub == "rm" else [os.path.join(*places)]
    elif name == "rsync" and any(a.startswith("--delete") for a in args):
        named = [a for a in args if a[:1] != "-"]
        if len(named) < 2 or ":" in named[-1]:
            return None  # no destination to resolve, or a remote one
        targets = named[-1:]  # the destination loses what the source lacks
    elif name == "zip" and any(a[:1] == "-" and a[1:2] != "-" and "m" in a for a in args):
        targets = [a for a in args if a[:1] != "-"][1:]  # after the archive
        if not targets:
            return None
    elif name == "tar" and "--remove-files" in args:
        return None
    elif name in _DELETERS:
        cut = args.index("--") if "--" in args else len(args)
        targets = [a for a in args[:cut] if a[:1] != "-" or a == "-"] + args[cut + 1:]
        if not targets:
            return None  # the paths arrive at run time
    else:
        return []
    if any(not t or _UNRESOLVED.search(t) for t in targets):
        return None
    return [os.path.realpath(os.path.join(cwd, t)) for t in targets]


def inside(paths: list[str], root: str) -> bool:
    """True only when every path is root or under it, both resolved through symlinks.

    False for an empty list: a delete with no known path is not known to stay inside.
    """
    top = os.path.realpath(root)
    return bool(paths) and all(os.path.commonpath([os.path.realpath(p), top]) == top for p in paths)


def _parse(text: str, depth: int) -> list[list[str]]:
    """The simple commands in the text, in the order they start, each peeled."""
    out: list[list[str]] = []
    _scan(text, 0, depth, out, nested=False)
    return out


def _scan(text: str, i: int, depth: int, out: list[list[str]], nested: bool) -> int:
    """Read the commands from i into out; when nested, stop after the ) that closes them."""
    words: list[_Word] = []
    heredocs: list[tuple[str, bool]] = []
    redirect, parens = "", 0
    while True:
        while i < len(text) and text[i] in " \t":
            i += 1
        if text.startswith("\\\n", i):
            i += 2
            continue
        if text.startswith("#", i):  # a comment runs to the end of its line
            i = text.find("\n", i) if "\n" in text[i:] else len(text)
            continue
        if text.startswith("((", i) and (end := _arithmetic(text, i + 2)) is not None:  # an arithmetic command: nothing runs
            i = end
            continue
        if text.startswith(("<(", ">("), i):
            start, i = i, _substitution(text, i + 2, depth, out)
            word: _Word = (text[start:i], True)
        elif operator := _REDIRECT.match(text, i):
            if redirect:
                raise _Unreadable
            redirect, i = operator.group().lstrip("0123456789"), operator.end()
            continue
        elif i >= len(text) or _SEPARATOR.match(text, i):
            separator = _SEPARATOR.match(text, i)
            if redirect or (nested and separator is None):
                raise _Unreadable  # an operator with no word after it, or a $( never closed
            if words:
                out.extend(_peel(words, depth))
                words = []
            if separator is None:
                return i
            i = separator.end()
            if separator.group() == "(":
                parens += 1
            elif separator.group() == ")":
                if nested and parens == 0:
                    return i
                parens -= 1
            elif separator.group() == "\n":
                for delimiter, tabs in heredocs:  # a heredoc body is data: skip to its last line
                    while i < len(text):
                        end = text.find("\n", i) if "\n" in text[i:] else len(text)
                        line, i = text[i:end], end + 1
                        if (line.lstrip("\t") if tabs else line) == delimiter:
                            break
                heredocs = []
            continue
        else:
            word, i = _word(text, i, depth, out)
        if redirect.startswith("<<") and redirect != "<<<":
            heredocs.append((word[0], redirect == "<<-"))
        if redirect:
            redirect = ""  # the word was where the redirection points, not an argument
        else:
            words.append(word)


def _word(text: str, i: int, depth: int, out: list[list[str]]) -> tuple[_Word, int]:
    """One word from i with its quotes removed, and the index after it."""
    chars: list[str] = []
    expands = quoted = False
    while i < len(text) and (quoted or text[i] not in _BREAKS):
        c = text[i]
        if c == "'" and not quoted:
            end = text.find("'", i + 1)
            if end < 0:
                raise _Unreadable
            chars.append(text[i + 1:end])
            i = end + 1
        elif c == '"':
            quoted, i = not quoted, i + 1
        elif c == "\\":
            if i + 1 >= len(text):
                raise _Unreadable
            escaped = text[i + 1]
            if escaped != "\n":  # a backslash before a newline joins the lines
                chars.append(c + escaped if quoted and escaped not in '$`"\\' else escaped)
            i += 2
        elif text.startswith("$((", i) and (end := _arithmetic(text, i + 3)) is not None:  # arithmetic: a number once expanded, never a command
            start, i = i, end
            chars.append(text[start:i])
            expands = True
        elif text.startswith("$(", i):
            start, i = i, _substitution(text, i + 2, depth, out)
            chars.append(text[start:i])
            expands = True
        elif c == "`":
            end = i + 1
            while end < len(text) and text[end] != "`":
                end += 2 if text[end] == "\\" else 1
            if end >= len(text) or depth == 0:
                raise _Unreadable
            out.extend(_parse(re.sub(r"\\([$`\\])", r"\1", text[i + 1:end]), depth - 1))
            chars.append(text[i:end + 1])
            expands, i = True, end + 1
        else:
            expands = expands or c == "$"
            chars.append(c)
            i += 1
    if quoted:
        raise _Unreadable
    return ("".join(chars), expands), i


def _arithmetic(text: str, i: int) -> int | None:
    """The index after the )) that closes an arithmetic (( whose body starts at i; None when the ) that closes the second (
    has no ) at once after it, which bash reads as two subshells (or $( and a subshell), not arithmetic."""
    open_ = 2
    while i < len(text) and open_:
        open_ += {"(": 1, ")": -1}.get(text[i], 0)
        i += 1
        if open_ == 1 and i < len(text) and text[i] != ")":
            return None
    if open_:
        raise _Unreadable
    return i


def _substitution(text: str, i: int, depth: int, out: list[list[str]]) -> int:
    """Read the commands of a $(, <( or >( whose body starts at i; the index after its )."""
    if depth == 0:
        raise _Unreadable
    return _scan(text, i, depth - 1, out, nested=True)


def _peel(argv: list[_Word], depth: int) -> list[list[str]]:
    """The commands one simple command stands for, with wrappers and shell words peeled."""
    while argv:
        word, name = argv[0][0], os.path.basename(argv[0][0])
        if _ASSIGNMENT.match(word) or word in _KEYWORDS:
            argv = argv[1:]
        elif word == "function":
            argv = argv[2:]
        elif word in ("for", "case", "select"):
            return []  # a loop or case header, not a command
        elif name in _SHELLS or name in ("eval", "su"):
            return _reread(argv, name, depth)
        elif name in _WRAPPERS:
            takes, before = _WRAPPERS[name]
            i = 1
            while i < len(argv) and argv[i][0].startswith("-"):
                option, i = argv[i][0], i + 1
                if option == "--":
                    break
                if option.startswith(("--chdir", "--split-string", "--login")):
                    raise _Unreadable
                if option in _LONG_VALUES:
                    i += 1
                for k, letter in enumerate("" if option.startswith("--") else option[1:], 2):
                    if (name, letter) in _HIDING:
                        raise _Unreadable
                    if name == "command" and letter in "vV":
                        return [[w for w, _ in argv]]  # a lookup: nothing runs
                    if letter in takes:
                        i += 1 if option[k:] == "" else 0
                        break
            if i + before >= len(argv):
                return [[w for w, _ in argv]]  # nothing wrapped: the wrapper itself runs
            if name == "xargs":
                return [found if _WHOLE.get() else found[:1] for found in _peel(argv[i:], depth)]
            argv = argv[i + before:]
        else:
            break
    if not argv:
        return []
    if argv[0][1]:
        raise _Unreadable  # the program is known only at run time
    found = [[w for w, _ in argv]]
    if os.path.basename(argv[0][0]) == "find":  # -exec and -ok run a command per file
        for i, (word, _) in enumerate(argv):
            if word in ("-exec", "-execdir", "-ok", "-okdir"):
                end = next((j for j in range(i + 1, len(argv)) if argv[j][0] in (";", "+")), len(argv))
                found += _peel(argv[i + 1:end], depth)
    return found


def _reread(argv: list[_Word], name: str, depth: int) -> list[list[str]]:
    """Read again the string that a shell's -c, su -c or eval would run, one level deeper."""
    words = [w for w, _ in argv]
    if name == "eval":
        string = (" ".join(words[1:]), any(e for _, e in argv[1:]))
    elif name == "su":
        if "-c" not in words[:-1] or {"-", "-l", "--login"} & set(words):
            raise _Unreadable  # a root shell that reads its input, or one that runs in root's home
        string = argv[words.index("-c") + 1]
    else:
        reads, i = False, 1
        while i < len(argv) and words[i][:1] in ("-", "+"):
            option, i = words[i], i + 1
            if option in ("-o", "+o", "-O", "+O", "--rcfile", "--init-file"):
                i += 1
            elif option[:2] != "--" and "s" in option:
                raise _Unreadable  # -s: it reads its commands from its input
            elif option[:2] != "--" and "c" in option:
                reads = True
        if not reads and i < len(argv):
            return [words]  # it runs a script file
        if not reads or i >= len(argv):
            raise _Unreadable  # it reads its commands from its input
        string = argv[i]
    if string[1] or depth == 0:
        raise _Unreadable  # the outer shell expands it first, or it is nested too deep
    return _parse(string[0], depth - 1)
