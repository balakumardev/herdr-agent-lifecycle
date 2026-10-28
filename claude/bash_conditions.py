"""A closed, non-executing subset of Claude's Bash hook condition matcher.

True/False are proven applicability results; None preserves the caller's unknown
gate. Claude can run every hook on complex syntax, so inspect the entire command
before matching anything. This is deliberately not a general shell interpreter.
"""
import re


_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\+?=)")
_REDIRECT = re.compile(r"(?:[0-9]+)?(<<-|<<|>>|>&|<&|>|<)")
_PATTERN = re.compile(r"[A-Za-z0-9_./:@%+=, ~*\t-]+")
_RAW_EQUALS = re.compile(r"(?:^|[\s;&|])=[a-zA-Z_]")
_RAW_CONTINUATION = re.compile(r"(?:^|[^ \t\\])(?:\\\\)*\\\n|[ \t](?:\\\\)+\\\n")
_BRACE_QUOTE = re.compile(r"\{[^}]*['\"]")
_SPECIAL_HEADS = frozenset((
    "if then elif else fi for while until do done case esac in select function "
    "time coproc eval source . export declare typeset local readonly unset alias unalias"
).split())


class _Unknown(ValueError):
    pass


def _mask_quoted_braces(text):
    """Mirror the native raw guard's quote mask, not shell execution semantics.

    Comments remain raw and heredocs are deliberately not recognized here.
    The guard runs before the literal parser can discard either kind of text.
    """
    if "{" not in text:
        return text
    result = []
    single = double = backtick = False
    boundary = True
    pos = 0
    while pos < len(text):
        char = text[pos]
        following = text[pos + 1] if pos + 1 < len(text) else None
        if backtick:
            if char == "\\" and following in ("`", "\\", "$"):
                result.append(text[pos:pos + 2])
                pos += 2
                continue
            if char == "`":
                backtick = False
            result.append(" " if char == "{" else char)
        elif single:
            if char == "'":
                single = False
            result.append(" " if char == "{" else char)
        elif double:
            if char == "\\" and following in ('"', "\\", "`"):
                result.append(text[pos:pos + 2])
                pos += 2
                continue
            if char == "`":
                backtick = True
            elif char == '"':
                double = False
            result.append(" " if char == "{" else char)
        elif char == "\\" and following is not None:
            result.append(text[pos:pos + 2])
            if following != "\n":
                boundary = False
            pos += 2
            continue
        elif char == "#" and boundary:
            end = text.find("\n", pos)
            end = len(text) if end == -1 else end
            result.append(text[pos:end])
            pos, boundary = end, True
            continue
        else:
            if char == "`":
                backtick, boundary = True, False
            else:
                single = char == "'"
                double = char == '"'
                boundary = char in " \t\n;|&()<>"
            result.append(char)
        pos += 1
    return "".join(result)


def _word(text, pos):
    """Read one static word, retaining whether quote removal protected any part."""
    start, chars, quote, protected = pos, [], None, False
    while pos < len(text):
        char = text[pos]
        if quote == "'":
            if char == quote:
                quote = None
            else:
                chars.append(char)
        elif char == "\\":
            if pos + 1 == len(text) or text[pos + 1].isspace():
                raise _Unknown()
            following = text[pos + 1]
            if quote == '"' and following not in '$`"\\':
                chars.append(char)
            else:
                chars.append(following)
                pos += 1
                protected = True
        elif quote == '"':
            if char == quote:
                quote = None
            elif char in "$`":
                raise _Unknown()
            else:
                chars.append(char)
        elif char in " \t\n;|&<>":
            break
        elif char in "'\"":
            quote, protected = char, True
        elif char in "$`(){}!*?[]" or (char in "#=" and pos == start):
            raise _Unknown()
        else:
            chars.append(char)
        pos += 1
    if quote or pos == start:
        raise _Unknown()
    return "".join(chars), pos, protected


def _heredoc_end(text, pos, pending):
    for delimiter, strip_tabs in pending:
        while pos < len(text):
            end = text.find("\n", pos)
            end = len(text) if end == -1 else end
            line = text[pos:end]
            pos = min(end + 1, len(text))
            if (line.lstrip("\t") if strip_tabs else line) == delimiter:
                break
        else:
            raise _Unknown()
    return pos


def _commands(text):
    commands, argv, pending = [], [], []
    pos, seen_word, seen_redirect, needs_right = 0, False, False, False
    while pos < len(text):
        char = text[pos]
        if char in " \t":
            pos += 1
            continue
        if text.startswith("\\\n", pos):
            # Only between existing words/chain operators: joining word parts
            # or leading with a continuation takes a different native path.
            if not seen_word and not needs_right:
                raise _Unknown()
            pos += 2
            continue
        if char == "#":
            end = text.find("\n", pos)
            pos = len(text) if end == -1 else end
            continue
        separator = next((op for op in ("&&", "||", "|&", "|", ";", "\n")
                          if text.startswith(op, pos)), None)
        if separator:
            if (not seen_word and separator != "\n") or (seen_redirect and not argv):
                raise _Unknown()
            if seen_word:
                if argv:
                    commands.append(" ".join(argv))
                argv, seen_word, seen_redirect = [], False, False
                needs_right = separator not in (";", "\n")
            pos += len(separator)
            if separator == "\n" and pending:
                if needs_right:
                    raise _Unknown()
                pos = _heredoc_end(text, pos, pending)
                pending = []
            continue
        redirect = _REDIRECT.match(text, pos)
        if redirect:
            operator = redirect.group(1)
            pos = redirect.end()
            while pos < len(text) and text[pos] in " \t":
                pos += 1
            target, pos, protected = _word(text, pos)
            if operator.startswith("<<"):
                if pending or not protected or not target or "\n" in target:
                    raise _Unknown()
                pending.append((target, operator == "<<-"))
            seen_redirect = True
            continue
        start = pos
        word, pos, _ = _word(text, pos)
        assignment = _ASSIGNMENT.match(text, start)
        # Native conservatively dispatches on these placements, even when a
        # shell could interpret them. Do not normalize them into a false skip.
        if seen_redirect and (argv or assignment):
            raise _Unknown()
        if not argv and assignment:
            if assignment.group(1) != "=":
                raise _Unknown()
        else:
            if not argv and (not word or word in _SPECIAL_HEADS or "=" in word):
                raise _Unknown()
            seen_redirect = False
            argv.append(word)
        seen_word, needs_right = True, False
    if pending or needs_right or (seen_redirect and not argv):
        raise _Unknown()
    if argv:
        commands.append(" ".join(argv))
    return commands


def _star_match(pattern, candidate):
    """Anchored star-only matching without regex backtracking."""
    pattern = re.sub(r"[ \t]+", " ", pattern.strip())
    candidate = re.sub(r"[ \t]+", " ", candidate)
    if pattern.count("*") == 1 and pattern.endswith(" *") and candidate == pattern[:-2]:
        return True
    parts = pattern.split("*")
    if len(parts) == 1:
        return candidate == pattern
    if not candidate.startswith(parts[0]):
        return False
    pos = len(parts[0])
    for part in parts[1:-1]:
        found = candidate.find(part, pos)
        if found == -1:
            return False
        pos = found + len(part)
    return candidate.endswith(parts[-1]) and len(candidate) - len(parts[-1]) >= pos


def match_bash_condition(pattern, command):
    """Match the contents of Bash(pattern); return None for anything unproven."""
    if (not isinstance(pattern, str) or not isinstance(command, str)
            or len(pattern) > 512 or len(command) > 10000
            or not _PATTERN.fullmatch(pattern) or "**" in pattern):
        return None
    prefix = pattern[:-2] if pattern.endswith(":*") else None
    if prefix is not None and (not prefix or "*" in prefix):
        return None
    # Native parsing also refuses several raw-text ambiguities before lexing.
    if any((c.isspace() and c not in " \t\n") or c in "\u200b\ufeff" or (ord(c) < 32 and c not in "\t\n")
           or 127 <= ord(c) <= 159 or 0xD800 <= ord(c) <= 0xDFFF for c in command):
        return None
    if (len(command.encode("utf-8")) > 10000 or re.search(r"\\[ \t]", command)
            or "~[" in command or re.search(r"<[0-9]*-[0-9]*>", command)
            or _RAW_EQUALS.search(command) or _RAW_CONTINUATION.search(command)
            or _BRACE_QUOTE.search(_mask_quoted_braces(command))):
        return None
    try:
        candidates = _commands(command)
    except _Unknown:
        return None
    for candidate in candidates:
        if prefix is not None:
            if any(candidate == p or candidate.startswith(p + " ")
                   for p in (prefix, "xargs " + prefix)):
                return True
        elif _star_match(pattern, candidate) or _star_match("xargs " + pattern, candidate):
            return True
    return False
